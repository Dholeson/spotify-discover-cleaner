@echo off
REM Discover Cleaner launcher for Windows.
REM   run.bat                  start the app (http://127.0.0.1:8888); the first start shows the Setup screen
REM   run.bat --demo           offline demo: simulated Spotify and the author's listening history
REM   run.bat --demo --reset   start the demo over
REM   run.bat --port 9000      serve on another port
REM Creates .venv on the first run and installs requirements.txt whenever it changes. No .env needed.
setlocal
cd /d "%~dp0"

REM --- Python 3.10 or newer: the "py" launcher first, then python on PATH ---
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set "PY=py -3"
if not defined PY (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo Discover Cleaner needs Python 3.10 or newer.
    echo Install it from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^), then run this again.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating a virtual environment in .venv ...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo Could not create the virtual environment. Check the error above.
        pause
        exit /b 1
    )
)

REM --- install requirements on the first run and whenever requirements.txt changes ---
fc /b requirements.txt ".venv\requirements.installed" >nul 2>&1
if errorlevel 1 (
    echo Installing dependencies ...
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
    if errorlevel 1 (
        echo.
        echo Dependency install failed. Check the error above.
        pause
        exit /b 1
    )
    copy /y requirements.txt ".venv\requirements.installed" >nul
)

".venv\Scripts\python.exe" app.py %*
if errorlevel 1 pause
endlocal
