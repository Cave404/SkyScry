@echo off
setlocal EnableDelayedExpansion
title SkyScry - Create Desktop Shortcuts

cd /d "%~dp0"
set "REPO_DIR=%~dp0"
if "%REPO_DIR:~-1%"=="\" set "REPO_DIR=%REPO_DIR:~0,-1%"

echo ============================================================================
echo   SkyScry v0.0.8 - Windows Desktop Shortcut Generator
echo ============================================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$repoDir = '%REPO_DIR%'; " ^
    "$desktopDir = [Environment]::GetFolderPath('Desktop'); " ^
    "$shell = New-Object -ComObject WScript.Shell; " ^
    "$s1Path = Join-Path $desktopDir 'SkyScry Weather.lnk'; " ^
    "$s1 = $shell.CreateShortcut($s1Path); " ^
    "$s1.TargetPath = Join-Path $repoDir 'Start_SkyScry.bat'; " ^
    "$s1.WorkingDirectory = $repoDir; " ^
    "$s1.Description = 'Start SkyScry 433.92 MHz Weather Daemon and open WebUI'; " ^
    "$s1.IconLocation = 'shell32.dll,13'; " ^
    "$s1.Save(); " ^
    "Write-Host ('[OK] Created Desktop shortcut: ' + $s1Path); " ^
    "$s2Path = Join-Path $desktopDir 'SkyScry Weather (Silent Background).lnk'; " ^
    "$s2 = $shell.CreateShortcut($s2Path); " ^
    "$s2.TargetPath = 'wscript.exe'; " ^
    "$s2.Arguments = '\"' + (Join-Path $repoDir 'Launch_SkyScry_Background.vbs') + '\"'; " ^
    "$s2.WorkingDirectory = $repoDir; " ^
    "$s2.Description = 'Launch SkyScry silently in the background (no console window)'; " ^
    "$s2.IconLocation = 'shell32.dll,13'; " ^
    "$s2.Save(); " ^
    "Write-Host ('[OK] Created Desktop shortcut: ' + $s2Path);"

echo.
echo Done! You can now start SkyScry directly from your Windows Desktop.
echo To stop the silent background service at any time, run Stop_SkyScry.bat.
echo.
pause
endlocal

