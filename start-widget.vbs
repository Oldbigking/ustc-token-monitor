' Launch the USTC token floating widget without a console window.
' Double-click to start it; copy this file into
'   %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
' to have it start with Windows (the widget itself needs monitor.py to be running).
On Error Resume Next
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = dir
sh.Run "pythonw.exe """ & dir & "\widget.py""", 0, False
If Err.Number <> 0 Then
    MsgBox "Cannot run pythonw.exe." & vbCrLf & vbCrLf & _
           "Either add Python to PATH, or edit this file and replace" & vbCrLf & _
           "pythonw.exe with the full path to your pythonw.exe, e.g." & vbCrLf & _
           "  C:\Python313\pythonw.exe", 16, "USTC token widget"
End If
