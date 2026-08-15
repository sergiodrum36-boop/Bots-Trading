@echo off
REM Ciclo diario de paper trading -- pensado para que lo lance el
REM Programador de tareas de Windows, NO para doble clic interactivo
REM (no se queda esperando ninguna tecla al terminar, a diferencia de
REM iniciar_panel.bat).
REM
REM Cada ejecucion queda registrada en logs\ciclo_AAAA-MM-DD.log (creado
REM si no existe) ademas de en la base de datos de auditoria
REM (trading_bot.dryrun.sqlite), que es la que lee el panel visual.
REM
REM Para probarlo a mano: abre una ventana de cmd, entra en esta carpeta
REM y escribe "ejecutar_ciclo_diario.bat" (si haces doble clic desde el
REM explorador, la ventana se cierra sola al terminar -- revisa el log).

cd /d "%~dp0"

if not exist "logs" mkdir "logs"

if not exist "venv\Scripts\activate.bat" (
    echo %date% %time% - No se encontro el entorno virtual "venv" en esta carpeta. >> "logs\ciclo_errores.log"
    exit /b 1
)

for /f "delims=" %%d in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set HOY=%%d

call venv\Scripts\activate.bat

python scripts\run_paper_trading.py --verbose >> "logs\ciclo_%HOY%.log" 2>&1

exit /b %errorlevel%
