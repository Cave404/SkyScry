@echo off
setlocal EnableDelayedExpansion
title SkyScry v0.0.9 - 433.92 MHz Weather Telemetry Daemon

:: 1. Ensure working directory is the SkyScry repository root
cd /d "%~dp0"

echo ============================================================================
echo   SkyScry v0.0.9 - 433.92 MHz Microclimate Telemetry Daemon
echo ============================================================================
echo.

:: 2. Clean up any orphaned RTL-SDR processes holding the USB handle
echo [1/5] Checking for orphaned RTL-SDR USB processes...
taskkill /F /IM rtl_433-rtlsdr.exe >nul 2>&1
taskkill /F /IM rtl_433.exe >nul 2>&1

:: 3. Ensure Python virtual environment (.venv) exists and is activated
if not exist ".venv\Scripts\python.exe" (
    echo [2/5] Creating Python virtual environment ^(.venv^)...
    python -m venv .venv
    if !ERRORLEVEL! NEQ 0 (
        echo [ERROR] Failed to create Python virtual environment. Ensure Python 3.10+ is installed and on your PATH.
        pause
        exit /b 1
    )
) else (
    echo [2/5] Activating Python virtual environment ^(.venv^)...
)
call ".venv\Scripts\activate.bat"

:: 4. Pull latest code from GitHub if this is a Git repository
if exist ".git" (
    where git >nul 2>&1
    if !ERRORLEVEL! EQU 0 (
        echo [3/5] Checking GitHub for latest SkyScry updates...
        git pull --rebase --autostash origin main
    ) else (
        echo [3/5] Git not found on PATH; skipping Git pull.
    )
) else (
    echo [3/5] Standalone folder detected ^(no .git directory^); skipping Git pull.
)

:: 5. Ensure pip and all required Python packages are installed and upgraded
echo [4/5] Verifying and upgrading Python dependencies...
python -m pip install --upgrade pip --quiet
python -m pip install --upgrade -r requirements.txt --quiet

:: 6. Launch SkyScry daemon and open the WebUI in the default browser
echo [5/5] Starting SkyScry daemon ^(WebUI: http://127.0.0.1:8080^)...
echo.
python run.py --open-browser %*

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] SkyScry exited with error code %ERRORLEVEL%.
    pause
)
endlocal

