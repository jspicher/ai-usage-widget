' Start the widget without a console window.
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
pythonw = fso.BuildPath(dir, ".venv\Scripts\pythonw.exe")
checkOnly = WScript.Arguments.Named.Exists("check")
If Not fso.FileExists(pythonw) Then
    If checkOnly Then
        WScript.Echo "missing: run install.bat"
        WScript.Quit 1
    End If
    MsgBox "The widget virtual environment is missing." & vbCrLf & vbCrLf & _
        "Run install.bat first, then launch start_widget.vbs again.", _
        vbExclamation, "AI Usage Widget"
    WScript.Quit 1
End If
If checkOnly Then
    WScript.Echo "ready"
    WScript.Quit 0
End If
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = dir
sh.Run """" & pythonw & """ """ & fso.BuildPath(dir, "widget.py") & """", 0, False
