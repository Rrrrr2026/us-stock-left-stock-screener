' run_hidden.vbs -- hidden-window wrapper for the Windows scheduled task (card PCHIDE, 2026-10-06).
'
' Task action:  C:\Windows\System32\wscript.exe "<this repo>\run_hidden.vbs"
'   (replaces the old action that ran auto_update.bat directly: that opened a visible cmd window
'    in the user's session, and closing that window by hand killed the run -- 10-04 audit.)
'
' What it does: runs   cmd.exe /c "<this folder>\auto_update.bat auto"   synchronously with
'   intWindowStyle = 0 (no window at all) and bWaitOnReturn = True, appends the bat's stdout/stderr
'   to <this folder>\logs\task_YYYYMMDD.log (the bat's own per-step log data\update.log is untouched),
'   and returns the bat's exit code unchanged to the Task Scheduler (LastTaskResult), so the bat's
'   ABORT paths (exit /b 1) stay visible there. Task logs older than KEEP_DAYS are deleted.
'   logs\ is gitignored. The folder is taken from this script's own location: no paths to edit.
'
' ASCII only on purpose: wscript reads .vbs in the ANSI code page (GBK on this PC).
Option Explicit
Const BAT_NAME = "auto_update.bat"
Const BAT_ARGS = "auto"
Const KEEP_DAYS = 60

Dim fso, sh, base, bat, logDir, logFile, cmd, rc, f, ts, stamp
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")

base = fso.GetParentFolderName(WScript.ScriptFullName)
bat = fso.BuildPath(base, BAT_NAME)
logDir = fso.BuildPath(base, "logs")
If Not fso.FolderExists(logDir) Then fso.CreateFolder logDir
stamp = Year(Now) & Right("0" & Month(Now), 2) & Right("0" & Day(Now), 2)
logFile = fso.BuildPath(logDir, "task_" & stamp & ".log")

' housekeeping: drop task_*.log older than KEEP_DAYS (never fatal)
On Error Resume Next
For Each f In fso.GetFolder(logDir).Files
  If LCase(Left(f.Name, 5)) = "task_" And LCase(Right(f.Name, 4)) = ".log" Then
    If DateDiff("d", f.DateLastModified, Now) > KEEP_DAYS Then f.Delete True
  End If
Next
On Error GoTo 0

Set ts = fso.OpenTextFile(logFile, 8, True)
If Not fso.FileExists(bat) Then
  ts.WriteLine "==== " & Now & " run_hidden.vbs: bat not found: " & bat & " (exit 2)"
  ts.Close
  WScript.Quit 2
End If
ts.WriteLine "==== " & Now & " run_hidden.vbs start: " & BAT_NAME & " " & BAT_ARGS & " (hidden window)"
ts.Close

sh.CurrentDirectory = base
cmd = "cmd.exe /c """"" & bat & """"
If Len(BAT_ARGS) > 0 Then cmd = cmd & " " & BAT_ARGS
cmd = cmd & " >> """ & logFile & """ 2>&1"""
rc = sh.Run(cmd, 0, True)

Set ts = fso.OpenTextFile(logFile, 8, True)
ts.WriteLine "==== " & Now & " run_hidden.vbs end: " & BAT_NAME & " exit code " & rc
ts.Close
WScript.Quit rc
