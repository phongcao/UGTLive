@echo off
chcp 65001 >nul 2>&1
setlocal ENABLEDELAYEDEXPANSION

set "SCRIPT_DIR=%~dp0"
set "CONFIG_FILE=%SCRIPT_DIR%service_config.txt"
set "LOG_FILE=%SCRIPT_DIR%setup_log.txt"
set "VENV_DIR=%SCRIPT_DIR%venv"
set "VERSION_FILE=%SCRIPT_DIR%service_install_version_last_installed.txt"

if exist "%LOG_FILE%" del "%LOG_FILE%"
echo ============================================================= >> "%LOG_FILE%"
echo Setup Log - %date% %time% >> "%LOG_FILE%"
echo ============================================================= >> "%LOG_FILE%"

set "ENV_NAME="
set "SERVICE_NAME="
set "SERVICE_INSTALL_VERSION=1"

if exist "%CONFIG_FILE%" (
    for /f "usebackq tokens=1,2 delims=| eol=#" %%a in ("%CONFIG_FILE%") do (
        set "KEY=%%a"
        set "VALUE=%%b"
        for /f "tokens=*" %%x in ("!KEY!") do set "KEY=%%x"
        for /f "tokens=*" %%y in ("!VALUE!") do set "VALUE=%%y"
        if "!KEY!"=="venv_name" set "ENV_NAME=!VALUE!"
        if "!KEY!"=="service_name" set "SERVICE_NAME=!VALUE!"
        if "!KEY!"=="service_install_version" set "SERVICE_INSTALL_VERSION=!VALUE!"
    )
)

if "!ENV_NAME!"=="" (
    echo ERROR: Could not find venv_name in service_config.txt
    echo ERROR: Could not find venv_name in service_config.txt >> "%LOG_FILE%"
    if not "%1"=="nopause" pause
    exit /b 1
)

if "!SERVICE_NAME!"=="" set "SERVICE_NAME=OCR Service"

echo =============================================================
echo   !SERVICE_NAME! Environment Setup
echo   Virtual Environment: !ENV_NAME!
echo =============================================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found in PATH
    echo ERROR: Python not found in PATH >> "%LOG_FILE%"
    if not "%1"=="nopause" pause
    exit /b 1
)

for /f "delims=" %%v in ('python --version 2^>^&1') do set "PY_VER_STR=%%v"
echo Detected !PY_VER_STR!
echo Detected !PY_VER_STR! >> "%LOG_FILE%"
echo !PY_VER_STR! | find "3.12" >nul
if errorlevel 1 (
    echo ERROR: Python 3.12 is required but found: !PY_VER_STR!
    echo ERROR: Wrong Python version: !PY_VER_STR! >> "%LOG_FILE%"
    if not "%1"=="nopause" pause
    exit /b 1
)

if exist "%VENV_DIR%" (
    rmdir /s /q "%VENV_DIR%"
)

python -m venv "%VENV_DIR%" >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo ERROR: Failed to create virtual environment
    echo ERROR: Failed to create virtual environment >> "%LOG_FILE%"
    if not "%1"=="nopause" pause
    exit /b 1
)

call "%VENV_DIR%\Scripts\activate.bat" >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo ERROR: Failed to activate virtual environment
    echo ERROR: Failed to activate virtual environment >> "%LOG_FILE%"
    if not "%1"=="nopause" pause
    exit /b 1
)

python -m pip install --upgrade pip setuptools wheel >> "%LOG_FILE%" 2>&1
python -m pip install fastapi uvicorn requests pillow certifi >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo ERROR: Failed to install dependencies
    echo ERROR: Failed to install dependencies >> "%LOG_FILE%"
    if not "%1"=="nopause" pause
    exit /b 1
)

echo !SERVICE_INSTALL_VERSION!>"%VERSION_FILE%"
echo Installation completed successfully
echo Installation completed successfully >> "%LOG_FILE%"
if not "%1"=="nopause" pause
exit /b 0