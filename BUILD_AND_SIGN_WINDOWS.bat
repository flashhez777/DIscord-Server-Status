@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title DGSM 3.3.3 - Automatic Windows EXE and DLL Builder

echo ============================================================
echo Discord Global Status Monitor 3.3.3
echo Automatic Python, MSVC, SDK, EXE, DLL and signing pipeline
echo ============================================================
echo.
where powershell.exe >nul 2>nul
if errorlevel 1 (
  echo ERROR: Windows PowerShell was not found. This builder requires Windows 10/11.
  set "BUILD_EXIT=9009"
  goto failed
)
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_windows.ps1" %*
set "BUILD_EXIT=%ERRORLEVEL%"
if not "%BUILD_EXIT%"=="0" goto failed

echo.
echo BUILD SUCCEEDED.
echo EXE, DLL, Authenticode signatures and package are under dist\windows\
echo.
pause
endlocal & exit /b 0

:failed
echo.
echo BUILD FAILED with exit code %BUILD_EXIT%.
echo Python runtime discovery no longer treats "No suitable Python runtime found" as a PowerShell exception.
echo See BUILD_TROUBLESHOOTING.txt and the full output above.
echo.
pause
endlocal & exit /b %BUILD_EXIT%
