@echo off
REM Lanzador del panel visual del bot de trading.
REM Doble clic sobre este archivo -- no hace falta escribir nada en una
REM terminal. Activa el entorno virtual del proyecto y abre el panel en
REM el navegador (Streamlit lo abre solo; si no, ve a http://localhost:8501).

cd /d "%~dp0"

if not exist "venv\Scripts\activate.bat" (
    echo No se encontro el entorno virtual "venv" en esta carpeta.
    echo Crealo primero con: python -m venv venv
    echo y luego instala las dependencias con: venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

call venv\Scripts\activate.bat

streamlit run dashboard\app.py

pause
