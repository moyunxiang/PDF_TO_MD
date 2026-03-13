@echo off
echo === PDF to Markdown Setup ===
echo.

REM Create virtual environment
python -m venv .venv
if errorlevel 1 (
    echo [ERROR] Failed to create venv. Make sure Python 3.10+ is installed.
    pause
    exit /b 1
)

REM Upgrade pip first
.venv\Scripts\pip install --upgrade pip

REM Install dependencies
.venv\Scripts\pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies.
    pause
    exit /b 1
)

echo.
echo === Setup complete! ===
echo Run: run.bat
pause
