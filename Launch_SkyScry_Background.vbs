' ============================================================================
' SkyScry v0.0.12 - Silent Background Launcher (No Console Window)
' ============================================================================
' Double-click this file to start the SkyScry daemon silently in the
' background and automatically open the WebUI in your default browser.
' To stop the background daemon, run Stop_SkyScry.bat.
' ============================================================================

Option Explicit

Dim objFSO, objShell, strRepoDir, strBatchPath, strCmd

Set objFSO = CreateObject("Scripting.FileSystemObject")
Set objShell = CreateObject("WScript.Shell")

strRepoDir = objFSO.GetParentFolderName(WScript.ScriptFullName)
objShell.CurrentDirectory = strRepoDir

strBatchPath = objFSO.BuildPath(strRepoDir, "Start_SkyScry.bat")
strCmd = "cmd.exe /c """ & strBatchPath & """"

' WindowStyle 0 = Hidden window, False = Do not wait for termination
objShell.Run strCmd, 0, False

Set objShell = Nothing
Set objFSO = Nothing

