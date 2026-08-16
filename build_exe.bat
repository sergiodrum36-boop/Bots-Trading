@echo off
REM Genera el .exe del panel (Etapa 2 del rediseno "Panel v2").
REM
REM Uso: doble clic, o desde una terminal cmd en esta carpeta.
REM Salida: dist\BotDeTrading\BotDeTrading.exe (+ archivos de apoyo).
REM
REM IMPORTANTE (leer antes de correrlo):
REM   - Este build es SOLO PARA WINDOWS -- tiene que correr en tu maquina
REM     Windows real, no se puede generar un .exe de Windows desde otro
REM     sistema operativo.
REM   - Empaquetar Streamlit con PyInstaller es conocido por ser delicado
REM     (ver tray_app.py para el razonamiento completo). Es razonable que
REM     el primer intento falle por algun detalle -- si el .exe no arranca
REM     o falla con un error de "No module named ..." o "No package
REM     metadata was found for ...", copia el error exacto y lo resolvemos
REM     juntos; normalmente se arregla anadiendo un --collect-all o
REM     --copy-metadata mas a la lista de abajo.
REM   - "Modo onedir" (no "onefile"): la salida es una CARPETA
REM     (dist\BotDeTrading\), no un unico archivo suelto. Arranca mas
REM     rapido y es mas facil de depurar que un .exe unico que se
REM     autoextrae en cada arranque -- para distribuir, comprime esa
REM     carpeta entera en un .zip.
REM   - COMPILA EN UNA CARPETA TEMPORAL LOCAL (fuera de Google Drive), y
REM     copia el resultado terminado aqui al final. Esto es a proposito:
REM     esta carpeta del proyecto vive dentro de Google Drive, y se
REM     confirmo (2026-08-16) que compilar directamente ahi puede dejar
REM     el .exe corrupto ("Could not load PyInstaller's embedded PKG
REM     archive from the executable") porque Drive y/o el antivirus
REM     tocan el archivo mientras PyInstaller todavia lo esta escribiendo.
REM     Nada de esto cambia donde queda el resultado final: sigue siendo
REM     dist\BotDeTrading\ dentro de esta carpeta, como siempre.
REM   - Los paquetes propios del proyecto (dashboard, config, connectors,
REM     core, execution, persistence, risk, strategy, backtesting) se
REM     empaquetan con --add-data (como datos sueltos), NO con
REM     --collect-all -- PyInstaller no los sabe embeber bien como modulos
REM     (no tienen __init__.py), y --collect-all los dejaba invisibles en
REM     tiempo de ejecucion ("ModuleNotFoundError: No module named
REM     'config'", visto en la practica el 2026-08-16). tray_app.py anade
REM     la carpeta donde quedan (`sys._MEIPASS`) a `sys.path` a mano antes
REM     de arrancar Streamlit para que se puedan importar con normalidad.
REM   - CONSECUENCIA del punto anterior: al ser --add-data (datos sueltos),
REM     PyInstaller NUNCA lee el codigo de esos paquetes propios para
REM     averiguar que otras librerias usan por dentro (pydantic, PyYAML,
REM     alpaca-py, tenacity, SQLAlchemy, psycopg2...) -- por eso estan
REM     todas anadidas a mano abajo con --collect-all. Si en el futuro se
REM     usa una libreria nueva desde dentro de esos paquetes y no desde
REM     tray_app.py directamente, hay que anadirla aqui tambien a mano (no
REM     se detecta sola).

cd /d "%~dp0"

if not exist "venv\Scripts\activate.bat" (
    echo No se encontro el entorno virtual "venv" en esta carpeta.
    echo Crealo primero con: python -m venv venv
    pause
    exit /b 1
)

call venv\Scripts\activate.bat

echo.
echo Instalando/actualizando dependencias del panel (requirements.txt) --
echo hace falta que esten instaladas de verdad en este venv para que
echo PyInstaller pueda empaquetarlas (pystray, Pillow, pywebview, etc.)...
pip install -r requirements.txt
if errorlevel 1 (
    echo Fallo instalando requirements.txt. Revisa el error de arriba.
    pause
    exit /b 1
)

echo.
echo Instalando/actualizando PyInstaller (requirements-build.txt)...
pip install -r requirements-build.txt
if errorlevel 1 (
    echo Fallo instalando PyInstaller. Revisa el error de arriba.
    pause
    exit /b 1
)

echo.
echo Generando el icono (assets\icon.ico) por si no existe o ha cambiado...
python assets\generate_icon.py

echo.
echo Cerrando BotDeTrading.exe si quedo abierto de un intento anterior
echo (si sigue corriendo, bloquea sus propios archivos/DLLs y el build
echo falla con "Permission denied" al intentar sobreescribirlos)...
taskkill /F /IM BotDeTrading.exe >nul 2>&1

echo.
echo Preparando una carpeta temporal local para compilar (fuera de Google
echo Drive) -- ver comentario "COMPILA EN UNA CARPETA TEMPORAL LOCAL" al
echo principio de este script...
set "LOCALBUILD=%LOCALAPPDATA%\BotDeTradingBuild"
if exist "%LOCALBUILD%" rmdir /s /q "%LOCALBUILD%"
mkdir "%LOCALBUILD%\build"
mkdir "%LOCALBUILD%\dist"
if exist BotDeTrading.spec del /q BotDeTrading.spec

echo.
echo Compilando con PyInstaller (esto puede tardar varios minutos)...
pyinstaller ^
    --name BotDeTrading ^
    --windowed ^
    --icon assets\icon.ico ^
    --paths . ^
    --workpath "%LOCALBUILD%\build" ^
    --distpath "%LOCALBUILD%\dist" ^
    --collect-all streamlit --copy-metadata streamlit ^
    --collect-all pystray ^
    --collect-all PIL ^
    --collect-all webview ^
    --collect-all pydantic --copy-metadata pydantic ^
    --collect-all pydantic_settings --copy-metadata pydantic-settings ^
    --collect-all alpaca --copy-metadata alpaca-py ^
    --collect-all yaml ^
    --collect-all tenacity ^
    --collect-all sqlalchemy ^
    --collect-all psycopg2 ^
    --add-data "dashboard;dashboard" ^
    --add-data "config;config" ^
    --add-data "connectors;connectors" ^
    --add-data "core;core" ^
    --add-data "execution;execution" ^
    --add-data "persistence;persistence" ^
    --add-data "risk;risk" ^
    --add-data "strategy;strategy" ^
    --add-data "backtesting;backtesting" ^
    --add-data ".streamlit\config.toml;.streamlit" ^
    --noconfirm ^
    tray_app.py

if errorlevel 1 (
    echo.
    echo PyInstaller fallo -- revisa el error de arriba y me lo pegas para
    echo que lo resolvamos juntos.
    pause
    exit /b 1
)

echo.
echo Copiando el build terminado desde la carpeta temporal local a esta
echo carpeta (dist\BotDeTrading) -- esto NUNCA borra tu .env real si ya
echo lo habias colocado ahi en una compilacion anterior, solo anade/
echo actualiza los archivos del build nuevo...
if not exist "dist" mkdir "dist"
robocopy "%LOCALBUILD%\dist\BotDeTrading" "dist\BotDeTrading" /E >nul
if errorlevel 8 (
    echo.
    echo Fallo copiando el resultado del build desde la carpeta temporal.
    echo Revisa el error de arriba y me lo pegas para resolverlo juntos.
    pause
    exit /b 1
)

echo.
echo Copiando archivos editables junto al .exe (config, .env de ejemplo,
echo tema visual, icono) -- para que se puedan ajustar sin recompilar...
if not exist "dist\BotDeTrading\config" mkdir "dist\BotDeTrading\config"
copy /Y "config\config.yaml" "dist\BotDeTrading\config\config.yaml" >nul
if not exist "dist\BotDeTrading\.streamlit" mkdir "dist\BotDeTrading\.streamlit"
copy /Y ".streamlit\config.toml" "dist\BotDeTrading\.streamlit\config.toml" >nul
if not exist "dist\BotDeTrading\assets" mkdir "dist\BotDeTrading\assets"
copy /Y "assets\icon.ico" "dist\BotDeTrading\assets\icon.ico" >nul
copy /Y ".env.example" "dist\BotDeTrading\.env.example" >nul

echo.
echo ============================================================
echo  Listo: dist\BotDeTrading\BotDeTrading.exe
echo.
echo  ANTES DE ABRIRLO: copia tu .env real (con tus claves de
echo  Alpaca) a la carpeta dist\BotDeTrading\ -- por seguridad este
echo  script NUNCA copia tu .env real ahi automaticamente, solo un
echo  .env.example vacio como plantilla.
echo ============================================================
echo.
pause
