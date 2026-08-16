@echo off
REM Lanzador del panel visual del bot de trading -- MODO NAVEGADOR/DEPURACION.
REM Desde la Etapa 1 del rediseno "Panel v2", el lanzador normal del dia a
REM dia es iniciar_panel_app.vbs (ventana de escritorio propia, sin cmd
REM visible). Este .bat se conserva para diagnosticar problemas de arranque
REM con la ventana de cmd (y sus mensajes de error) visibles, o si prefieres
REM seguir usando el navegador.
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
