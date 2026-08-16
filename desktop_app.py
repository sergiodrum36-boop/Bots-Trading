"""Lanzador de escritorio del panel del bot de trading (Etapa 1 del
rediseno "Panel v2").

Envuelve `dashboard/app.py` (Streamlit) en su propia ventana de escritorio
con `pywebview`, en vez de depender de que el usuario abra el navegador a
mano. Pensado para lanzarse SIN mostrar ninguna ventana de cmd -- ver
`iniciar_panel_app.vbs` (usa pythonw.exe, que no tiene consola, mas
WScript.Shell en modo oculto como capa extra de seguridad).

Que hace, en orden:
  1. Si no hay ya algo escuchando en el puerto local de Streamlit, arranca
     `streamlit run dashboard/app.py` en un proceso hijo, en modo
     "headless" (sin que Streamlit intente abrir el navegador el solo).
  2. Espera a que el servidor responda (con reintentos cortos, hasta un
     limite de tiempo razonable).
  3. Abre una ventana pywebview apuntando a ese servidor local, con su
     propio titulo (no una pestana de navegador).
  4. Al cerrar la ventana, mata el proceso de Streamlit que arranco este
     script -- si no, quedaria corriendo en segundo plano ocupando el
     puerto (habria que matarlo a mano desde el Administrador de tareas
     antes de poder volver a abrir el panel).

La logica del panel en si (dashboard/app.py, dashboard/data.py) no cambia
nada aqui -- esto es solo "como se lanza y se muestra". El icono de
ventana/bandeja del sistema queda para la Etapa 2 (empaquetado con
PyInstaller en un .exe), donde tiene mas sentido fijarlo de una vez.

Requiere el paquete `pywebview` (ver requirements.txt) y, en Windows, el
runtime WebView2 -- viene preinstalado de serie con Windows 10/11 junto
con Edge, asi que no deberia hacer falta instalarlo aparte.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import webview

PROJECT_ROOT = Path(__file__).resolve().parent
HOST = "localhost"
PORT = 8501
STARTUP_TIMEOUT_SECONDS = 30
WINDOW_TITLE = "Bot de Trading"


def _ensure_no_streamlit_welcome_prompt() -> None:
    """Crea un credentials.toml vacio si no existe, para que Streamlit no
    se quede esperando un email por consola la primera vez que se ejecuta
    en esta maquina -- lanzado via pywebview no hay ninguna terminal
    visible donde el usuario pueda responder ese prompt (ver la nota
    sobre este mismo problema, ya visto con el lanzamiento por navegador,
    en la memoria del Project)."""
    credentials_path = Path.home() / ".streamlit" / "credentials.toml"
    if credentials_path.exists():
        return
    credentials_path.parent.mkdir(parents=True, exist_ok=True)
    credentials_path.write_text('[general]\nemail = ""\n', encoding="utf-8")


def _port_is_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def _start_streamlit() -> subprocess.Popen:
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(PROJECT_ROOT / "dashboard" / "app.py"),
            "--server.headless",
            "true",
            "--server.address",
            HOST,
            "--server.port",
            str(PORT),
            "--browser.gatherUsageStats",
            "false",
        ],
        cwd=str(PROJECT_ROOT),
        creationflags=creationflags,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_for_server(process: subprocess.Popen) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                "Streamlit se cerro solo antes de terminar de arrancar -- "
                "revisa que el entorno virtual tenga las dependencias "
                "instaladas (venv\\Scripts\\pip install -r requirements.txt)."
            )
        if _port_is_open(HOST, PORT):
            return
        time.sleep(0.3)
    raise TimeoutError(
        f"Streamlit no respondio en {STARTUP_TIMEOUT_SECONDS}s -- revisa "
        f"si el puerto {PORT} ya esta en uso por otro proceso."
    )


def _show_error_window(message: str) -> None:
    # Sin terminal visible para leer el error, lo mostramos en su propia
    # ventana en vez de perderlo en un stderr que nadie va a ver.
    safe_message = message.replace("<", "&lt;").replace(">", "&gt;")
    webview.create_window(
        f"{WINDOW_TITLE} -- Error al arrancar",
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
    _ensure_no_streamlit_welcome_prompt()

    process: subprocess.Popen | None = None
    if _port_is_open(HOST, PORT):
        # Ya hay algo escuchando en ese puerto -- lo mas probable es una
        # instancia de Streamlit que quedo corriendo de una sesion
        # anterior que no se cerro bien. Nos conectamos a esa en vez de
        # arrancar una segunda y chocar por el puerto.
        process = None
    else:
        process = _start_streamlit()
        try:
            _wait_for_server(process)
        except (RuntimeError, TimeoutError) as exc:
            process.terminate()
            _show_error_window(str(exc))
            return

    window = webview.create_window(
        WINDOW_TITLE,
        url=f"http://{HOST}:{PORT}",
        width=1440,
        height=900,
        min_size=(1000, 700),
    )

    def _on_closing() -> None:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()

    window.events.closing += _on_closing

    webview.start()


if __name__ == "__main__":
    main()
