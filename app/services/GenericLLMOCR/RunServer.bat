@echo off
chcp 65001 >nul 2>&1
setlocal ENABLEDELAYEDEXPANSION

set "SCRIPT_DIR=%~dp0"
set "CONFIG_FILE=%SCRIPT_DIR%service_config.txt"
set "VENV_DIR=%SCRIPT_DIR%venv"
set "LOG_DIR=%SCRIPT_DIR%logs"
set "LOG_FILE=%SCRIPT_DIR%server_log.txt"
set "PYTHON_EXE=%VENV_DIR%\Scripts\python.exe"
set "SERVICE_PORT=5005"
set "SESSION_STAMP="

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
        if "!KEY!"=="port" set "SERVICE_PORT=!VALUE!"
    )
)

if "!ENV_NAME!"=="" (
    echo ERROR: Could not find venv_name in service_config.txt
    if not "%1"=="nopause" pause
    exit /b 1
)

if "!SERVICE_NAME!"=="" set "SERVICE_NAME=OCR Service"

for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "(Get-Date).ToString('yyyyMMdd_HHmmss')"`) do set "SESSION_STAMP=%%i"
if "!SESSION_STAMP!"=="" set "SESSION_STAMP=manual_%RANDOM%"
set "SESSION_LOG_FILE=%LOG_DIR%\console_!SESSION_STAMP!_%RANDOM%.log"

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" >nul 2>&1

set "SERVICE_ALREADY_RUNNING="
for /f "delims=" %%i in ('powershell -NoProfile -Command "try { $resp = Invoke-RestMethod -TimeoutSec 2 -Uri 'http://127.0.0.1:!SERVICE_PORT!/info'; if (($resp.service_name -eq '!SERVICE_NAME!') -or ($resp.service -eq '!SERVICE_NAME!')) { 'running' } } catch {}"') do set "SERVICE_ALREADY_RUNNING=%%i"

if /i "!SERVICE_ALREADY_RUNNING!"=="running" (
    echo !SERVICE_NAME! is already running on http://127.0.0.1:!SERVICE_PORT!
    echo Existing service detected at %date% %time% > "%SESSION_LOG_FILE%"
    echo Reused running service instead of starting a second instance. >> "%SESSION_LOG_FILE%"
    goto :eof
)

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

if not exist "%PYTHON_EXE%" (
    echo ERROR: Python executable not found at: %PYTHON_EXE%
    echo Please run Install.bat first.
    if not "%1"=="nopause" pause
    exit /b 1
)

call "%VENV_DIR%\Scripts\activate.bat"
for /f "delims=" %%i in ('python -c "import certifi; print(certifi.where())" 2^>nul') do set "SSL_CERT_FILE=%%i"
set "SSL_CERT_DIR="

echo Session log: %SESSION_LOG_FILE%
echo. >> "%SESSION_LOG_FILE%"
echo ============================================================= >> "%SESSION_LOG_FILE%"
echo Server Start - %date% %time% >> "%SESSION_LOG_FILE%"
echo Python Executable - %PYTHON_EXE% >> "%SESSION_LOG_FILE%"
echo ============================================================= >> "%SESSION_LOG_FILE%"
"%PYTHON_EXE%" -u "%SCRIPT_DIR%server.py" >> "%SESSION_LOG_FILE%" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
echo Server Exit Code - !EXIT_CODE! at %date% %time% >> "%SESSION_LOG_FILE%"
if not "!EXIT_CODE!"=="0" (
    echo.
    echo ERROR: Server crashed or failed to start
    echo See log: %SESSION_LOG_FILE%
    if not "%1"=="nopause" pause
    exit /b 1
)

goto :eof