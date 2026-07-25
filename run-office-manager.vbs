Option Explicit

Dim shell, fileSystem, appFolder, pythonw, appFile
Set shell = CreateObject("WScript.Shell")
Set fileSystem = CreateObject("Scripting.FileSystemObject")

appFolder = fileSystem.GetParentFolderName(WScript.ScriptFullName)
pythonw = fileSystem.BuildPath(appFolder, ".venv\Scripts\pythonw.exe")
appFile = fileSystem.BuildPath(appFolder, "office_manager.pyw")

If Not fileSystem.FileExists(pythonw) Then
    MsgBox "The app is not set up yet. Run setup.bat first.", vbExclamation, "Kateb Office Data Manager"
    WScript.Quit 1
End If

shell.CurrentDirectory = appFolder
shell.Run """" & pythonw & """ """ & appFile & """", 0, False
