@echo off
setlocal
set "ROOT=%~dp0"
if exist "%ROOT%dist\windows\exe\DiscordGlobalStatusMonitor.exe" (
  "%ROOT%dist\windows\exe\DiscordGlobalStatusMonitor.exe" %*
  exit /b %errorlevel%
)
if exist "%ROOT%dist\windows\DiscordGlobalStatusMonitor.exe" (
  "%ROOT%dist\windows\DiscordGlobalStatusMonitor.exe" %*
  exit /b %errorlevel%
)
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 "%ROOT%discord_server_global_status.py" %*
  exit /b %errorlevel%
)
where python >nul 2>nul
if %errorlevel%==0 (
  python "%ROOT%discord_server_global_status.py" %*
  exit /b %errorlevel%
)
echo Python 3 is required. Install Python 3.9 or newer, then run this launcher again.
pause
exit /b 1
