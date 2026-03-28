@echo off
chcp 65001 >nul 2>&1
setlocal ENABLEDELAYEDEXPANSION

set "SCRIPT_DIR=%~dp0"
set "CONFIG_FILE=%SCRIPT_DIR%service_config.txt"
set "VENV_DIR=%SCRIPT_DIR%venv"
set "LOG_FILE=%SCRIPT_DIR%server_log.txt"

set "ENV_NAME="
set "SERVICE_NAME="

if exist "%CONFIG_FILE%" (
    for /f "usebackq tokens=1,2 delims=| eol=#" %%a in ("%CONFIG_FILE%") do (
        set "KEY=%%a"
        set "VALUE=%%b"
        for /f "tokens=*" %%x in ("!KEY!") do set "KEY=%%x"
        for /f "tokens=*" %%y in ("!VALUE!") do set "VALUE=%%y"
        if "!KEY!"=="venv_name" set "ENV_NAME=!VALUE!"
        if "!KEY!"=="service_name" set "SERVICE_NAME=!VALUE!"
    )
)

if "!ENV_NAME!"=="" (
    echo ERROR: Could not find venv_name in service_config.txt
    if not "%1"=="nopause" pause
    exit /b 1
)

if "!SERVICE_NAME!"=="" set "SERVICE_NAME=OCR Service"

echo =============================================================
echo   Starting !SERVICE_NAME!
echo   Environment: !ENV_NAME!
echo =============================================================
echo.

if not exist "%VENV_DIR%\Scripts\activate.bat" (
    echo ERROR: Virtual environment not found at: %VENV_DIR%
    echo Please run Install.bat first.
    if not "%1"=="nopause" pause
    exit /b 1
)

call "%VENV_DIR%\Scripts\activate.bat"
for /f "delims=" %%i in ('python -c "import certifi; print(certifi.where())"') do set "SSL_CERT_FILE=%%i"
set "SSL_CERT_DIR="

echo ============================================================= > "%LOG_FILE%"
echo Server Start - %date% %time% >> "%LOG_FILE%"
echo ============================================================= >> "%LOG_FILE%"
python "%SCRIPT_DIR%server.py" >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo.
    echo ERROR: Server crashed or failed to start
    echo See log: %LOG_FILE%
    if not "%1"=="nopause" pause
    exit /b 1
)

goto :eof