@echo off
setlocal
cd /d "%~dp0"
title Discord Global Status Monitor v3.3.3

rem Prefer the signed standalone Windows build when present.
if exist "%~dp0DiscordGlobalStatusMonitor.exe" (
    "%~dp0DiscordGlobalStatusMonitor.exe" %*
    set "EXIT_CODE=%ERRORLEVEL%"
    goto finished
)
if exist "%~dp0dist\windows\exe\DiscordGlobalStatusMonitor.exe" (
    "%~dp0dist\windows\exe\DiscordGlobalStatusMonitor.exe" %*
    set "EXIT_CODE=%ERRORLEVEL%"
    goto finished
)

rem Source-mode fallback for development / unbuilt package.
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -c "import sys" >nul 2>&1
    if not errorlevel 1 (
        py -3 discord_server_global_status.py %*
        set "EXIT_CODE=%ERRORLEVEL%"
        goto finished
    )
)

where python >nul 2>nul
if not errorlevel 1 (
    python -c "import sys" >nul 2>&1
    if not errorlevel 1 (
        python discord_server_global_status.py %*
        set "EXIT_CODE=%ERRORLEVEL%"
        goto finished
    )
)

echo A working Python runtime was not found and no compiled EXE is present.
echo Run BUILD_AND_SIGN_WINDOWS.bat to install build dependencies and compile it.
echo Or install Python from https://www.python.org/downloads/windows/
set "EXIT_CODE=9009"

:finished
echo.
if not "%EXIT_CODE%"=="0" echo Monitor exited with code %EXIT_CODE%.
pause
endlocal & exit /b %EXIT_CODE%
