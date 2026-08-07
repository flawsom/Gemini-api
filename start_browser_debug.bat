@echo off
REM start_browser_debug.bat - open the default Chromium browser with a CDP
REM debug port so gemini-web2api can process images WITHOUT the extension,
REM even while the browser is open.
REM
REM The debug port can only be enabled when the browser LAUNCHES, so if it is
REM already running you must close it first, then run this file again.
REM Your browser will restore your tabs when it reopens.

setlocal
set PORT=9401
set URL=https://gemini.google.com/app

REM Find the default browser exe (Brave/Chrome/Edge/Vivaldi).
set "EXE="
if exist "%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe" set "EXE=%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe"
if not defined EXE if exist "%LocalAppData%\BraveSoftware\Brave-Browser\Application\brave.exe" set "EXE=%LocalAppData%\BraveSoftware\Brave-Browser\Application\brave.exe"
if not defined EXE if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "EXE=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not defined EXE if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "EXE=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not defined EXE if exist "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe" set "EXE=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
if not defined EXE if exist "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" set "EXE=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"

if not defined EXE (
    echo Could not find Brave, Chrome or Edge. Install one of them and re-run.
    pause
    exit /b 1
)

for %%F in ("%EXE%") do set "BROWSER=%%~nxF"

REM If the browser is already running, the debug port cannot be attached.
tasklist 2>NUL | find /I "%BROWSER%" >NUL
if not errorlevel 1 (
    echo.
    echo The browser is already open. The debug port can only be enabled at
    echo launch, so please close all its windows, then double-click this
    echo file again. It will restore your session when it reopens.
    echo.
    pause
    exit /b 2
)

echo Launching %BROWSER% with CDP debug port %PORT% ...
start "" "%EXE%" --remote-debugging-port=%PORT% --remote-allow-origins=* "%URL%"
echo.
echo Done. The debug port is active for this browser session - image requests
echo will now work without the extension. Leave the browser running.
echo To verify: open http://127.0.0.1:%PORT%/json/version in any browser.
echo.
pause
