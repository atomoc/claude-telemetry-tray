On Error Resume Next
Dim fso, sh, dir, script
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
script = dir & "\claude-telemetry-tray.py"
sh.Run "pythonw.exe """ & script & """", 0, False
If Err.Number <> 0 Then
  Err.Clear
  sh.Run "py -w """ & script & """", 0, False
  If Err.Number <> 0 Then
    Err.Clear
    sh.Run "python """ & script & """", 0, False
    If Err.Number <> 0 Then
      MsgBox "Could not start Python. Run start-tray-windows.bat to see the error.", 48, "Claude Telemetry"
    End If
  End If
End If
