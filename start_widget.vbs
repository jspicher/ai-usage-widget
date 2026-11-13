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
' /toggle hides the widget if it is showing, for any launcher that wants a
' show/hide switch. F15 no longer goes through here: the running widget
' registers it as a global hotkey itself (see GlobalHotkey in widget.py).
extra = ""
If WScript.Arguments.Named.Exists("toggle") Then extra = " --toggle"
sh.Run """" & pythonw & """ """ & fso.BuildPath(dir, "widget.py") & """" & extra, 0, False
