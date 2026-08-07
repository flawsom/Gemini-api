@echo off
rem ============================================================
rem  start_server.bat - one double-click and you're done:
rem    1. Starts the Gemini Web2API server silently in the background
rem    2. Installs Windows auto-start at login (so you never click again)
rem ============================================================
setlocal EnableExtensions
cd /d "%~dp0"
set "PATH=%SystemRoot%\system32;%SystemRoot%;%PATH%"

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY where py >nul 2>nul && set "PY=py"
if not defined PY (
    echo [ERROR] Python not found. Install Python 3.9+ and add it to PATH.
    pause
    exit /b 1
)

call "%~dp0manage.bat" start

rem Install auto-start at login (silently skips if already installed)
"%PY%" "%~dp0autostart.py" status >nul 2>&1
if errorlevel 1 (
    echo.
    echo Setting up auto-start at login...
    call "%~dp0manage.bat" install
)

echo.
echo ------------------------------------------------------------
echo   Server:  http://localhost:8081/v1
echo   Control: double-click manage.bat or run manage.bat status
echo   Logs:    server.log and watchdog.log in this folder
echo ------------------------------------------------------------
echo.
ping -n 4 127.0.0.1 >nul
