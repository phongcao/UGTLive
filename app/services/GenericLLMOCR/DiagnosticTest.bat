@echo off
chcp 65001 >nul 2>&1
setlocal ENABLEDELAYEDEXPANSION

set "SCRIPT_DIR=%~dp0"
set "VENV_DIR=%SCRIPT_DIR%venv"

echo =============================================================
echo   Generic LLM OCR Diagnostic Test
echo =============================================================
echo.

if not exist "%VENV_DIR%\Scripts\activate.bat" (
    echo [FAIL] Virtual environment does not exist
    if not "%1"=="nopause" pause
    exit /b 1
)

call "%VENV_DIR%\Scripts\activate.bat"
python --version
python -c "import fastapi, uvicorn, requests, PIL, certifi; print('Imports OK')" 2>nul
if errorlevel 1 (
    echo [FAIL] Dependency import failed
    if not "%1"=="nopause" pause
    exit /b 1
)

echo [PASS] Diagnostic checks completed
if not "%1"=="nopause" pause
exit /b 0