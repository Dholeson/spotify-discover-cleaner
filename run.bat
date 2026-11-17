@echo off
REM Quick launcher for Windows. Creates venv on first run, installs deps, starts app.
setlocal

cd /d "%~dp0"

if not exist .venv (
    echo Creating virtual environment...
    python -m venv .venv
)

call .venv\Scripts\activate.bat

if not exist .venv\.deps_installed (
    echo Installing dependencies...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo Dependency install failed. Check the error above.
        pause
        exit /b 1
    )
    echo. > .venv\.deps_installed
)

if not exist .env (
    echo.
    echo No .env found. Copy .env.example to .env and fill in your Spotify credentials first.
    echo See README.md for instructions.
    pause
    exit /b 1
)

python app.py
pause
