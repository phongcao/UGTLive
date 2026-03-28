@echo off
chcp 65001 >nul 2>&1
setlocal ENABLEDELAYEDEXPANSION

set "SCRIPT_DIR=%~dp0"
set "CONFIG_FILE=%SCRIPT_DIR%service_config.txt"
set "VENV_DIR=%SCRIPT_DIR%venv"
set "VERSION_FILE=%SCRIPT_DIR%service_install_version_last_installed.txt"

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

if "!SERVICE_NAME!"=="" set "SERVICE_NAME=OCR Service"

echo =============================================================
echo   !SERVICE_NAME! - Remove Virtual Environment
echo =============================================================
echo.

if exist "%VENV_DIR%" (
    rmdir /s /q "%VENV_DIR%"
)

if exist "%VERSION_FILE%" del /q "%VERSION_FILE%"

if not "%1"=="nopause" pause
exit /b 0