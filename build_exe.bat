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

cd /d "%~dp0"

if not exist "venv\Scripts\activate.bat" (
    echo No se encontro el entorno virtual "venv" en esta carpeta.
    echo Crealo primero con: python -m venv venv
    pause
    exit /b 1
)

call venv\Scripts\activate.bat

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
echo Limpiando builds anteriores (build\, dist\, *.spec) para partir de
echo cero y que los cambios de este script se apliquen de verdad...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist BotDeTrading.spec del /q BotDeTrading.spec

echo.
echo Compilando con PyInstaller (esto puede tardar varios minutos)...
pyinstaller ^
    --name BotDeTrading ^
    --windowed ^
    --icon assets\icon.ico ^
    --paths . ^
    --collect-all streamlit --copy-metadata streamlit ^
    --collect-all dashboard ^
    --collect-all config ^
    --collect-all connectors ^
    --collect-all core ^
    --collect-all execution ^
    --collect-all persistence ^
    --collect-all risk ^
    --collect-all strategy ^
    --collect-all backtesting ^
    --add-data "dashboard\app.py;dashboard" ^
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
