' Runs run_dashboard.bat with no visible console window. Use this from a
' desktop/taskbar shortcut; double-click run_dashboard.bat directly instead
' when debugging a startup failure, since errors here have nowhere to show.
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = scriptDir
shell.Run """" & scriptDir & "\run_dashboard.bat""", 0, False
