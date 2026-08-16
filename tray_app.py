"""Panel del bot de trading como app de escritorio CON icono en la
bandeja del sistema de Windows (Etapa 2 del rediseno "Panel v2" -- ver
desktop_app.py para la Etapa 1, sin bandeja, que sigue funcionando igual
para uso en modo desarrollo).

Este es el punto de entrada que se empaqueta con PyInstaller (ver
build_exe.bat) para producir el .exe instalable. TAMBIEN funciona sin
empaquetar, corriendo directamente con `python tray_app.py` desde el venv
-- util para probar el comportamiento de la bandeja antes de generar el
.exe (mucho mas rapido de iterar).

Diferencias clave frente a desktop_app.py (Etapa 1):
  - Icono en la bandeja del sistema (pystray) con menu "Abrir panel" /
    "Salir".
  - Cerrar la ventana (la X) la OCULTA en vez de cerrar la app -- el panel
    sigue disponible desde la bandeja. Solo "Salir" desde la bandeja
    termina el proceso de verdad (y con el, el servidor de Streamlit).
  - Streamlit se arranca como PROCESO HIJO via `multiprocessing` llamando
    directamente a `streamlit.web.cli.main()` (la misma funcion que usa
    `streamlit run` desde la terminal), en vez de lanzar
    `sys.executable -m streamlit run ...` como subproceso (asi es como lo
    hacia desktop_app.py). Ese segundo enfoque NO funciona una vez
    empaquetado: un .exe de PyInstaller no tiene un interprete de Python
    normal detras de `sys.executable` al que poder pedirle "-m streamlit"
    -- el .exe ES el interprete empaquetado. El patron de
    `multiprocessing.Process` + `stcli.main()` si funciona en ambos casos
    (modo desarrollo Y modo "congelado"), y es el que usa el proyecto
    streamlit-desktop-app (ohtaman/streamlit-desktop-app) para resolver
    exactamente este mismo problema -- ver tambien build_exe.bat para el
    razonamiento completo de como se empaqueta.

IMPORTANTE -- esto no se ha podido probar de punta a punta como .exe real
todavia (esta sesion no tiene forma de compilar ni ejecutar un .exe de
Windows): el diseño sigue el patron ya probado de streamlit-desktop-app,
pero es razonable que el primer build en tu maquina falle por algun
detalle de empaquetado -- si pasa, pega aqui el error exacto y lo
resolvemos juntos.
"""

from __future__ import annotations

import multiprocessing
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pystray
import webview
from PIL import Image
from streamlit.web import cli as stcli

APP_TITLE = "Bot de Trading"
HOST = "localhost"
STARTUP_TIMEOUT_SECONDS = 30


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _app_dir() -> Path:
    """Carpeta de referencia para todo lo "externo" al bundle: `.env`,
    `config/config.yaml` y `assets/icon.ico`. En modo congelado es la
    carpeta donde vive el .exe (build_exe.bat copia ahi una plantilla de
    cada uno tras compilar, para que sean editables sin recompilar); en
    modo desarrollo es esta misma carpeta del proyecto."""
    if _is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _dashboard_script_path() -> Path:
    """Ruta al archivo REAL de dashboard/app.py que `streamlit run`
    necesita leer como fichero de texto (no como modulo importado --
    Streamlit ejecuta el script que se le indica, no lo importa). En modo
    congelado, PyInstaller expone los archivos anadidos via `--add-data`
    bajo `sys._MEIPASS` -- esto es igual en compilacion `--onefile` y
    `--onedir` (ver build_exe.bat)."""
    if _is_frozen():
        return Path(sys._MEIPASS) / "dashboard" / "app.py"  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent / "dashboard" / "app.py"


def _prepare_environment() -> None:
    """Deja listo todo lo que dependa de rutas ANTES de arrancar
    Streamlit. Streamlit corre en un PROCESO hijo (ver `_run_streamlit`),
    que hereda el `os.environ` del proceso padre en el momento de
    arrancar -- basta con dejarlo bien puesto aqui una sola vez."""
    app_dir = _app_dir()
    os.chdir(app_dir)

    # .env NUNCA se empaqueta dentro del .exe (son secretos) -- vive junto
    # al .exe, y el usuario lo copia ahi el mismo (ver build_exe.bat, que
    # copia .env.example pero jamas el .env real). Se carga aqui de forma
    # explicita, ANTES de que connectors/alpaca_connector.py lo intente
    # por su cuenta: en modo congelado, la busqueda automatica de
    # python-dotenv (que sube desde el __file__ del modulo que llama a
    # load_dotenv) no encontraria el .env real, porque ese modulo vive
    # dentro del bundle, no junto al .exe.
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=app_dir / ".env", override=False)

    # config/config.yaml junto al .exe, editable sin recompilar -- ver
    # config/settings.py: si el archivo no existe todavia (recien
    # instalado, antes de que el usuario copie el suyo), `load_settings()`
    # cae a los valores por defecto de siempre (caso seguro, no rompe
    # nada).
    os.environ.setdefault("TRADING_BOT_CONFIG_PATH", str(app_dir / "config" / "config.yaml"))


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("", 0))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return sock.getsockname()[1]


def _run_streamlit(script_path: str, port: int) -> None:
    """Punto de entrada del proceso hijo (`multiprocessing.Process`, ver
    `main()`). Llama a `stcli.main()`, la misma funcion que ejecuta
    `streamlit run` desde la terminal, simulando `sys.argv` -- funciona
    igual en modo desarrollo y "congelado" porque no depende de invocar un
    interprete de Python aparte.

    IMPORTANTE (modo congelado): este proceso hijo es RELANZADO desde cero
    por `multiprocessing` (metodo "spawn" en Windows) -- no hereda el
    `sys.path` en memoria del proceso padre, arranca con el que le da el
    bootloader de PyInstaller. `dashboard/app.py` importa paquetes propios
    del proyecto (`config`, `connectors`, `core`, etc, ver build_exe.bat --
    se empaquetan como datos sueltos con `--add-data`, no via
    `--collect-all`, porque son namespace packages sin `__init__.py` y
    PyInstaller no los sabe embeber bien en el PYZ) que solo se encuentran
    si `sys._MEIPASS` (la carpeta `_internal`, donde quedan esos datos)
    esta en `sys.path` -- por eso se añade aqui explicitamente antes de
    arrancar Streamlit; sin esto, `dashboard/app.py` falla con
    `ModuleNotFoundError: No module named 'config'` (visto en la practica
    el 2026-08-16) en cuanto Streamlit intenta ejecutarlo."""
    if _is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass and meipass not in sys.path:
            sys.path.insert(0, meipass)

    sys.argv = [
        "streamlit",
        "run",
        script_path,
        f"--server.port={port}",
        f"--server.address={HOST}",
        "--server.headless=true",
        "--global.developmentMode=false",
        "--browser.gatherUsageStats=false",
    ]
    stcli.main()


def _wait_for_port(port: int, timeout: float = STARTUP_TIMEOUT_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            if sock.connect_ex((HOST, port)) == 0:
                return
        time.sleep(0.3)
    raise TimeoutError(
        f"Streamlit no respondio en {timeout}s. Si esto pasa como .exe "
        f"(y no en modo desarrollo), lo mas probable es que falte algun "
        f"paquete en el empaquetado -- ver build_exe.bat."
    )


def _tray_icon_image() -> Image.Image:
    return Image.open(_app_dir() / "assets" / "icon.ico")


def _show_startup_error(message: str) -> None:
    # Sin terminal visible para leer el error (la app arranca sin
    # consola, ver build_exe.bat), lo mostramos en su propia ventana.
    safe_message = message.replace("<", "&lt;").replace(">", "&gt;")
    webview.create_window(
        f"{APP_TITLE} -- Error al arrancar",
        html=(
            "<div style=\"font-family:sans-serif;padding:24px;"
            "white-space:pre-wrap;background:#0F1320;color:#E5E7EB;"
            "height:100%;\">"
            f"{safe_message}</div>"
        ),
        width=640,
        height=320,
    )
    webview.start()


def main() -> None:
    # Imprescindible en Windows con PyInstaller + multiprocessing -- debe
    # ser la primera linea del bloque main, antes de cualquier otra cosa.
    multiprocessing.freeze_support()

    _prepare_environment()
    port = _find_free_port()
    script_path = str(_dashboard_script_path())

    streamlit_process = multiprocessing.Process(
        target=_run_streamlit, args=(script_path, port), daemon=True
    )
    streamlit_process.start()

    try:
        _wait_for_port(port)
    except TimeoutError as exc:
        streamlit_process.terminate()
        _show_startup_error(str(exc))
        return

    window = webview.create_window(
        APP_TITLE,
        url=f"http://{HOST}:{port}",
        width=1440,
        height=900,
        min_size=(1000, 700),
    )

    state = {"quitting": False}

    def _on_closing() -> bool:
        """Al pulsar la X de la ventana: oculta en vez de cerrar (minimiza
        a la bandeja) salvo que se este saliendo de verdad desde el menu
        de la bandeja. Devolver False cancela el cierre real (API de
        pywebview)."""
        if state["quitting"]:
            return True
        window.hide()
        return False

    window.events.closing += _on_closing

    def _show_window(icon: pystray.Icon | None = None, item: object | None = None) -> None:
        window.show()

    def _quit_app(icon: pystray.Icon, item: object | None = None) -> None:
        state["quitting"] = True
        icon.stop()
        streamlit_process.terminate()
        streamlit_process.join(timeout=5)
        if streamlit_process.is_alive():
            streamlit_process.kill()
        window.destroy()

    tray_icon = pystray.Icon(
        "bot_de_trading",
        icon=_tray_icon_image(),
        title=APP_TITLE,
        menu=pystray.Menu(
            pystray.MenuItem("Abrir panel", _show_window, default=True),
            pystray.MenuItem("Salir", _quit_app),
        ),
    )
    threading.Thread(target=tray_icon.run, daemon=True).start()

    webview.start()


if __name__ == "__main__":
    main()
