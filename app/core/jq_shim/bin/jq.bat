@echo off
REM Python jq shim - routes jq calls to Python implementation
REM This allows shell scripts to use jq syntax while processing is handled by Python

REM Get the directory where this script is located
set "SCRIPT_DIR=%~dp0"

REM Find the app root (4 levels up: bin -> jq_shim -> core -> app -> root)
pushd "%SCRIPT_DIR%\..\..\..\..\"
set "APP_ROOT=%CD%"
popd

REM Set PYTHONPATH to include app root
if defined PYTHONPATH (
    set "PYTHONPATH=%APP_ROOT%;%PYTHONPATH%"
) else (
    set "PYTHONPATH=%APP_ROOT%"
)

REM Execute the Python jq shim module
python -m app.core.jq_shim %*
