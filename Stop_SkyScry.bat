@echo off
setlocal EnableDelayedExpansion
title Stop SkyScry Daemon

cd /d "%~dp0"

echo ============================================================================
echo   Stopping SkyScry Background Daemon and Releasing RTL-SDR USB Port
echo ============================================================================
echo.

echo [1/2] Stopping SkyScry Python daemon processes ^(run.py^)...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$procs = Get-CimInstance Win32_Process | Where-Object { ($_.Name -like 'python*') -and ($_.CommandLine -like '*run.py*') }; " ^
    "if ($procs) { foreach ($p in $procs) { Write-Host ('  -> Terminating PID ' + $p.ProcessId + ' (' + $p.Name + ')'); Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue } } " ^
    "else { Write-Host '  -> No running SkyScry Python process found.' }"

echo [2/2] Stopping RTL-SDR subprocesses ^(rtl_433^)...
taskkill /F /IM rtl_433-rtlsdr.exe >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo   -^> Terminated rtl_433-rtlsdr.exe
) else (
    echo   -^> No running rtl_433-rtlsdr.exe process found.
)
taskkill /F /IM rtl_433.exe >nul 2>&1

echo.
echo [OK] SkyScry has been stopped and port 8080 / RTL-SDR USB handle are released.
timeout /t 3 >nul
endlocal

