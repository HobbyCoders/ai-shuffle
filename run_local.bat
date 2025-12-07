@echo off
REM AI Hub Local Mode Launcher for Windows
REM Usage: run_local.bat [--setup]

setlocal EnableDelayedExpansion

echo.
echo     _    ___   _   _ _   _ ____
echo    / \  ^|_ _^| ^| ^| ^| ^| ^| ^| ^| __ )
echo   / _ \  ^| ^|  ^| ^|_^| ^| ^| ^| ^|  _ \
echo  / ___ \ ^| ^|  ^|  _  ^| ^|_^| ^| ^|_) ^|
echo /_/   \_\___^| ^|_^| ^|_^|\___/^|____/
echo.
echo Local Mode Launcher (Windows)
echo.

REM Get the directory where this script is located
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

REM Check if Python is available
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found. Please install Python 3.10+ from https://python.org
    pause
    exit /b 1
)

REM Check Python version
for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo [OK] Python %PYVER% detected

REM Check for --setup flag
if "%1"=="--setup" goto :setup

REM Check if venv exists
if not exist "venv\Scripts\python.exe" (
    echo [INFO] Virtual environment not found. Running setup first...
    goto :setup
)

REM Check if frontend is built
if not exist "app\static\index.html" (
    echo [INFO] Frontend not built. Running setup first...
    goto :setup
)

goto :start

:setup
echo.
echo [INFO] Running first-time setup...
echo.

REM Create virtual environment
if not exist "venv\Scripts\python.exe" (
    echo [INFO] Creating virtual environment...
    python -m venv venv
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to create virtual environment
        pause
        exit /b 1
    )
    echo [OK] Virtual environment created
)

REM Activate venv and install dependencies
echo [INFO] Installing Python dependencies...
call venv\Scripts\activate.bat
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install dependencies
    pause
    exit /b 1
)
echo [OK] Dependencies installed

REM Build frontend if npm is available
where npm >nul 2>&1
if %errorlevel% equ 0 (
    if not exist "app\static\index.html" (
        echo [INFO] Building frontend...
        cd frontend
        npm install
        npm run build
        cd ..
        if exist "frontend\build" (
            if exist "app\static" rmdir /s /q "app\static"
            xcopy /e /i /q "frontend\build" "app\static"
            echo [OK] Frontend built
        )
    )
) else (
    echo [WARN] npm not found - skipping frontend build
    echo [INFO] Install Node.js from https://nodejs.org if you need the web UI
)

REM Check Claude CLI
where claude >nul 2>&1
if %errorlevel% equ 0 (
    echo [OK] Claude CLI found
) else (
    echo [WARN] Claude CLI not found
    echo [INFO] Install with: npm install -g @anthropic-ai/claude-code
)

echo.
echo [OK] Setup complete!
echo.

if "%1"=="--setup" (
    echo Run 'run_local.bat' to start the server
    pause
    exit /b 0
)

:start
echo [INFO] Starting AI Hub...
echo [INFO] Server will be available at http://127.0.0.1:8000
echo [INFO] Press Ctrl+C to stop
echo.

REM Set environment variables for local mode
set LOCAL_MODE=true
set HOST=127.0.0.1
set PORT=8000
set COOKIE_SECURE=false

REM Start the server
call venv\Scripts\activate.bat
python -m app.main

pause
