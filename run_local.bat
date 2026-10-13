@echo off
setlocal
cd /d "%~dp0"
if exist "venv\Scripts\python.exe" (
    "venv\Scripts\python.exe" run_local.py %*
    exit /b
)
where python >nul 2>&1
if errorlevel 1 (
    echo Python 3.12+ is required. Install it, then run run_local.bat --setup.
    exit /b 1
)
python run_local.py %*
exit /b
