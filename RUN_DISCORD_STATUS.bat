@echo off
setlocal
cd /d "%~dp0"
title Discord Global Status Monitor v3.1.0

where py >nul 2>nul
if not errorlevel 1 (
    py -3 discord_server_global_status.py %*
    set "EXIT_CODE=%ERRORLEVEL%"
    goto finished
)

where python >nul 2>nul
if not errorlevel 1 (
    python discord_server_global_status.py %*
    set "EXIT_CODE=%ERRORLEVEL%"
    goto finished
)

echo Python 3.9 or newer was not found.
echo Install Python from https://www.python.org/downloads/windows/
echo Enable "Add python.exe to PATH" during installation.
set "EXIT_CODE=9009"

:finished
echo.
if not "%EXIT_CODE%"=="0" echo Monitor exited with code %EXIT_CODE%.
pause
endlocal & exit /b %EXIT_CODE%
