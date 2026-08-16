' Lanzador de escritorio del panel del bot de trading (Etapa 1 del
' rediseno "Panel v2").
'
' Doble clic sobre este archivo -- no abre ninguna ventana de cmd (ni
' siquiera un parpadeo), a diferencia de un .bat normal. Usa pythonw.exe
' del entorno virtual del proyecto (variante de Python sin consola) para
' arrancar desktop_app.py, que a su vez levanta Streamlit en segundo plano
' y lo muestra en su propia ventana.
'
' Si el panel no llega a abrirse, revisa que el entorno virtual "venv"
' exista en esta misma carpeta y tenga las dependencias instaladas
' (venv\Scripts\pip install -r requirements.txt, incluye pywebview) --
' puedes usar iniciar_panel.bat (con ventana de cmd visible, para ver
' mensajes de error) para diagnosticarlo.

Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
pythonwPath = scriptDir & "\venv\Scripts\pythonw.exe"
appPath = scriptDir & "\desktop_app.py"

If Not fso.FileExists(pythonwPath) Then
    MsgBox "No se encontro el entorno virtual 'venv' en esta carpeta." & vbCrLf & _
           "Crealo primero con: python -m venv venv" & vbCrLf & _
           "y luego instala las dependencias con:" & vbCrLf & _
           "venv\Scripts\pip install -r requirements.txt", vbCritical, "Bot de Trading"
    WScript.Quit 1
End If

Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = scriptDir
' 0 = ventana oculta, False = no esperar a que termine (el panel se queda
' abierto hasta que el usuario cierra su ventana).
shell.Run """" & pythonwPath & """ """ & appPath & """", 0, False
