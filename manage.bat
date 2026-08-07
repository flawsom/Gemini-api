@echo off
rem ============================================================
rem  manage.bat - control center for the Gemini Web2API server
rem ============================================================
rem  Usage:
rem    manage.bat start       start the server silently in the background
rem    manage.bat stop        stop the server (and its watchdog)
rem    manage.bat restart     restart the server
rem    manage.bat status      is it running? + one-glance health summary
rem    manage.bat health      health summary only (cookie age, BL, 405s, refresh)
rem    manage.bat logs        tail the last 40 lines of server.log + watchdog.log
rem    manage.bat watch       run the watchdog in THIS window (see live logs)
rem    manage.bat cookies     refresh Gemini cookies automatically via your browser
rem    manage.bat install     auto-start the server silently at Windows login
rem    manage.bat uninstall   remove the auto-start entry (and stop)
rem    manage.bat help        show this help
rem ============================================================
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

rem Normalize PATH so Windows tools (netstat, where, timeout, schtasks...)
rem resolve even when this script is launched from unusual environments.
set "PATH=%SystemRoot%\system32;%SystemRoot%;%PATH%"

set "SVC=GeminiWeb2API"
set "PORT=8081"
set "PY="
set "PYW="

rem ---------- locate Python ----------
where python >nul 2>nul && set "PY=python"
if not defined PY where py >nul 2>nul && set "PY=py"
if not defined PY (
    echo [ERROR] Python was not found. Install Python 3.9+ and add it to PATH.
    exit /b 1
)
where pythonw >nul 2>nul && set "PYW=pythonw"

set "CMD=%~1"
if "%CMD%"=="" set "CMD=status"

if /i "%CMD%"=="start"     goto :start
if /i "%CMD%"=="stop"      goto :stop
if /i "%CMD%"=="restart"   goto :restart
if /i "%CMD%"=="status"    goto :status
if /i "%CMD%"=="health"    goto :health
if /i "%CMD%"=="logs"      goto :logs
if /i "%CMD%"=="watch"     goto :watch
if /i "%CMD%"=="cookies"    goto :cookies
if /i "%CMD%"=="install"   goto :install
if /i "%CMD%"=="uninstall" goto :uninstall
if /i "%CMD%"=="help"      goto :help
echo Unknown command: %1
goto :help

rem ============================================================
:is_running
rem Sets RUNNING=1 if something listens on %PORT%.
set "RUNNING=0"
netstat -ano | findstr /R /C:":%PORT% " | findstr /R /C:"LISTENING" >nul 2>nul
if not errorlevel 1 set "RUNNING=1"
exit /b 0

rem ============================================================
:start
call :is_running
if "%RUNNING%"=="1" (
    echo Gemini Web2API is already running on http://localhost:%PORT%/v1
    echo   status:  manage.bat status      logs:  manage.bat logs
    exit /b 0
)
if defined PYW (
    echo Starting Gemini Web2API silently in the background...
    start "" "%PYW%" "%~dp0watchdog.py" --port %PORT% --config "%~dp0config.json"
) else (
    echo pythonw not found - starting in a minimized console window...
    start "Gemini Web2API Server" /min "%PY%" "%~dp0watchdog.py" --port %PORT% --config "%~dp0config.json"
)
rem give it a moment to boot, then confirm
ping -n 4 127.0.0.1 >nul
call :is_running
if "%RUNNING%"=="1" (
    echo Server is UP on http://localhost:%PORT%/v1
    echo It keeps itself alive - logs go to server.log, watchdog.log
) else (
    echo Server did not come up within ~3s. Check logs:  manage.bat logs
)
exit /b 0

rem ============================================================
:stop
rem Safe stop, delegated to autostart.py: the watchdog PID is verified to be
rem really ours (command line contains watchdog.py) before it is killed, and
rem only the process owning our port is stopped. No cmd quoting traps here.
"%PY%" "%~dp0autostart.py" stop-watchdog %PORT%
exit /b 0

rem ============================================================
:restart
call :stop
ping -n 2 127.0.0.1 >nul
call :start
exit /b 0

rem ============================================================
:status
call :is_running
if "%RUNNING%"=="1" (
    echo STATUS: RUNNING on http://localhost:%PORT%/v1
    netstat -ano | findstr /R /C:":%PORT% " | findstr /R /C:"LISTENING"
) else (
    echo STATUS: not running
)
if exist "%~dp0watchdog.pid" (
    echo watchdog:  running - see watchdog.pid
) else (
    echo watchdog:  not running
)
"%PY%" "%~dp0autostart.py" status
echo.
echo ---- health ----
"%PY%" "%~dp0autostart.py" health %PORT%

exit /b 0

rem ============================================================
:health
"%PY%" "%~dp0autostart.py" health %PORT%
exit /b 0

rem ============================================================
:logs
echo ---- server.log (last 40 lines) ----
if exist "%~dp0server.log" (
    powershell -NoProfile -Command "Get-Content -Tail 40 -Path '%~dp0server.log'"
) else (
    echo (no server.log yet)
)
echo.
echo ---- watchdog.log (last 15 lines) ----
if exist "%~dp0watchdog.log" (
    powershell -NoProfile -Command "Get-Content -Tail 15 -Path '%~dp0watchdog.log'"
) else (
    echo (no watchdog.log yet)
)
exit /b 0

rem ============================================================
:watch
echo Watching in this window - close it to stop the server. Ctrl+C also stops.
"%PY%" "%~dp0watchdog.py" --port %PORT% --config "%~dp0config.json" --foreground
exit /b 0

rem ============================================================
:cookies
echo Refreshing Gemini cookies using your default browser...
echo   A new browser window will open, cookies update automatically,
echo   then only that window closes - your other tabs stay untouched.
"%PY%" "%~dp0cookie_autorefresh.py"
exit /b 0

rem ============================================================
:install
echo Installing auto-start at login...
"%PY%" "%~dp0autostart.py" install
if errorlevel 1 exit /b 1
echo.
echo Done - the server will now come back silently after every reboot.
exit /b 0


rem ============================================================
:uninstall
call :stop
"%PY%" "%~dp0autostart.py" uninstall
exit /b 0

rem ============================================================
:help
echo Gemini Web2API - manage.bat
echo.
echo   start       start the server silently in the background
echo   stop        stop the server (and its watchdog)
echo   restart     restart the server
echo   status      is it running? + one-glance health summary
echo   health      health summary only (cookie age, BL, 405s, refresh)
echo   logs        tail the last lines of server.log + watchdog.log
echo   watch       run the watchdog in THIS window (see live logs)
echo   cookies     refresh Gemini cookies automatically via your browser
echo   install     auto-start the server silently at Windows login
echo   uninstall   remove the auto-start entry (and stop)
echo   help        show this help
exit /b 0
