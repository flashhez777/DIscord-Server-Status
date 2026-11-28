@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Discord Global Status Monitor v3.4.4

rem Prefer a packaged standalone executable. This also supports running the
rem source checkout after BUILD_AND_SIGN_WINDOWS.bat has populated dist\windows.
set "MONITOR_EXE=%~dp0DiscordGlobalStatusMonitor.exe"
if not exist "%MONITOR_EXE%" set "MONITOR_EXE=%~dp0dist\windows\exe\DiscordGlobalStatusMonitor.exe"
if exist "%MONITOR_EXE%" (
  echo Starting compiled Discord Global Status Monitor...
  "%MONITOR_EXE%" %*
  set "EXIT_CODE=%ERRORLEVEL%"
  goto finished
)

rem Probe the launcher without starting the monitor; a stale legacy py.exe can
rem exist on PATH even when it has no usable Python runtime.
where py.exe >nul 2>nul
if not errorlevel 1 (
  py.exe -3 --version >nul 2>nul
  if not errorlevel 1 (
    py.exe -3 "%~dp0discord_server_global_status.py" %*
    set "EXIT_CODE=%ERRORLEVEL%"
    goto finished
  )
  echo Python Launcher exists but has no usable default runtime; trying python.exe...
)

where python.exe >nul 2>nul
if not errorlevel 1 (
  python.exe --version >nul 2>nul
  if not errorlevel 1 (
    python.exe "%~dp0discord_server_global_status.py" %*
    set "EXIT_CODE=%ERRORLEVEL%"
    goto finished
  )
)

echo Python 3.9 or newer was not found and no compiled EXE is available.
echo Install Python from https://www.python.org/downloads/windows/
echo The build launcher can install a supported build runtime automatically.
set "EXIT_CODE=9009"

:finished
echo.
if not "%EXIT_CODE%"=="0" echo Monitor exited with code %EXIT_CODE%.
pause
endlocal & exit /b %EXIT_CODE%
