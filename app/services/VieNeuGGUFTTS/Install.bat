@echo off
REM Force UTF-8 encoding for international compatibility
chcp 65001 >nul 2>&1
setlocal ENABLEDELAYEDEXPANSION

set "SCRIPT_DIR=%~dp0"
set "CONFIG_FILE=%SCRIPT_DIR%service_config.txt"
set "LOG_FILE=%SCRIPT_DIR%setup_log.txt"
set "VENV_DIR=%SCRIPT_DIR%venv"
set "NOPAUSE=%~1"

REM Delete existing log file
if exist "%LOG_FILE%" del "%LOG_FILE%"

REM Create log file and add header
echo ============================================================= >> "%LOG_FILE%"
echo Setup Log - %date% %time% >> "%LOG_FILE%"
echo ============================================================= >> "%LOG_FILE%"
echo. >> "%LOG_FILE%"

REM -----------------------------------------------------------------
REM Parse service_config.txt to get environment name and service name
REM -----------------------------------------------------------------
set "ENV_NAME="
set "SERVICE_NAME="
set "SERVICE_INSTALL_VERSION="

if exist "%CONFIG_FILE%" (
    for /f "usebackq tokens=1,2 delims=| eol=#" %%a in ("%CONFIG_FILE%") do (
        set "KEY=%%a"
        set "VALUE=%%b"
        
        REM Trim leading/trailing spaces
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
    pause
    exit /b 1
)

if "!SERVICE_NAME!"=="" set "SERVICE_NAME=VieNeu-GGUF-TTS"

echo ============================================================= >> "%LOG_FILE%"
echo Service: !SERVICE_NAME! >> "%LOG_FILE%"
echo Environment: !ENV_NAME! >> "%LOG_FILE%"
echo ============================================================= >> "%LOG_FILE%"
echo. >> "%LOG_FILE%"

echo =============================================================
echo   !SERVICE_NAME! Environment Setup
echo   Virtual Environment: !ENV_NAME!
echo =============================================================
echo.

REM -----------------------------------------------------------------
REM Find Python executable from system PATH
REM -----------------------------------------------------------------
set "PYTHON_VERSION=3.11"
set "PYTHON_EXE=python"
where python >nul 2>&1
if errorlevel 1 (
    echo.
    echo ERROR: Python not found in PATH. Please install Python !PYTHON_VERSION! and ensure it is in your PATH.
    echo ERROR: Python not found in PATH >> "%LOG_FILE%"
    echo.
    pause
    exit /b 1
)

for /f "delims=" %%v in ('python --version 2^>^&1') do set "PY_VER_STR=%%v"
echo Detected !PY_VER_STR! >> "%LOG_FILE%"
echo Detected !PY_VER_STR!

for /f "delims=" %%p in ('where python') do (
    set "PYTHON_EXE=%%p"
    goto :FoundPython
)
:FoundPython
echo Python executable: !PYTHON_EXE! >> "%LOG_FILE%"

REM -----------------------------------------------------------------
REM Remove existing venv if present
REM -----------------------------------------------------------------
echo [1/4] Removing existing virtual environment if present...
echo [1/4] Removing existing venv... >> "%LOG_FILE%"

if exist "%VENV_DIR%" (
    echo Removing existing venv directory...
    echo Removing existing venv directory... >> "%LOG_FILE%"
    rmdir /s /q "%VENV_DIR%"
    if exist "%VENV_DIR%" (
        echo ERROR: Failed to remove existing venv directory
        echo ERROR: Failed to remove venv directory >> "%LOG_FILE%"
        pause
        exit /b 1
    )
    echo Removed existing venv >> "%LOG_FILE%"
) else (
    echo No existing venv found >> "%LOG_FILE%"
)

REM -----------------------------------------------------------------
REM Create new virtual environment
REM -----------------------------------------------------------------
echo [2/4] Creating new virtual environment...
echo [2/4] Creating new venv... >> "%LOG_FILE%"

"!PYTHON_EXE!" -m venv "%VENV_DIR%" >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo.
    echo ERROR: Failed to create virtual environment
    echo ERROR: Failed to create venv >> "%LOG_FILE%"
    echo.
    pause
    exit /b 1
)
echo Virtual environment created successfully >> "%LOG_FILE%"

REM -----------------------------------------------------------------
REM Activate virtual environment
REM -----------------------------------------------------------------
echo [3/4] Activating virtual environment...
echo [3/4] Activating venv... >> "%LOG_FILE%"

call "%VENV_DIR%\Scripts\activate.bat" >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo.
    echo ERROR: Failed to activate virtual environment
    echo ERROR: Failed to activate venv >> "%LOG_FILE%"
    echo.
    pause
    exit /b 1
)
echo Virtual environment activated successfully >> "%LOG_FILE%"

set HF_HUB_DISABLE_SYMLINKS_WARNING=1
set KMP_DUPLICATE_LIB_OK=TRUE

REM -----------------------------------------------------------------
REM Install dependencies
REM -----------------------------------------------------------------
echo [4/4] Installing dependencies...
echo [4/4] Installing dependencies... >> "%LOG_FILE%"

echo [Step 1/5] Upgrading pip...
python -m pip install --upgrade pip setuptools wheel >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo ERROR: Failed to upgrade pip!
    pause
    exit /b 1
)

echo [Step 2/5] Installing VieNeu SDK (for codec, phonemizer, voices)...
echo [Step 2/5] Installing vieneu... >> "%LOG_FILE%"
python -m pip install --upgrade vieneu --extra-index-url https://pnnbao97.github.io/llama-cpp-python-v0.3.16/cpu/ >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo ERROR: Failed to install vieneu!
    echo ERROR: Failed to install vieneu >> "%LOG_FILE%"
    pause
    exit /b 1
)
echo vieneu installed successfully >> "%LOG_FILE%"

echo [Step 3/5] Installing HuggingFace transformers (for tokenizer)...
echo [Step 3/5] Installing transformers... >> "%LOG_FILE%"
python -m pip install --upgrade transformers huggingface-hub >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo ERROR: Failed to install transformers!
    echo ERROR: Failed to install transformers >> "%LOG_FILE%"
    pause
    exit /b 1
)
echo transformers installed successfully >> "%LOG_FILE%"

echo [Step 4/5] Installing FastAPI, Uvicorn, httpx, and audiotsm...
echo [Step 4/5] Installing FastAPI/Uvicorn/httpx/audiotsm... >> "%LOG_FILE%"
python -m pip install fastapi "uvicorn[standard]" certifi numpy httpx audiotsm >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo ERROR: Failed to install FastAPI/Uvicorn/httpx!
    echo ERROR: Failed to install FastAPI/Uvicorn/httpx >> "%LOG_FILE%"
    pause
    exit /b 1
)
echo FastAPI/Uvicorn/httpx installed successfully >> "%LOG_FILE%"

echo [Step 5/5] Verifying installation...
echo Verifying installation... >> "%LOG_FILE%"

python -c "from vieneu import Vieneu; print('VieNeu SDK imported successfully')" >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo WARNING: VieNeu SDK verification failed - model will download on first use
    echo WARNING: VieNeu SDK verification failed >> "%LOG_FILE%"
) else (
    echo VieNeu SDK verification passed >> "%LOG_FILE%"
)

python -c "from transformers import AutoTokenizer; print('transformers imported successfully')" >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo ERROR: transformers verification failed!
    echo ERROR: transformers verification failed >> "%LOG_FILE%"
    pause
    exit /b 1
)
echo transformers verification passed >> "%LOG_FILE%"

python -c "import fastapi; print('FastAPI imported successfully')" >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo ERROR: FastAPI verification failed!
    echo ERROR: FastAPI verification failed >> "%LOG_FILE%"
    pause
    exit /b 1
)
echo FastAPI verification passed >> "%LOG_FILE%"

python -c "import httpx; print('httpx imported successfully')" >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo ERROR: httpx verification failed!
    echo ERROR: httpx verification failed >> "%LOG_FILE%"
    pause
    exit /b 1
)
echo httpx verification passed >> "%LOG_FILE%"

echo. >> "%LOG_FILE%"
echo ============================================================= >> "%LOG_FILE%"
echo SUCCESS! Setup completed successfully >> "%LOG_FILE%"
echo End time: %date% %time% >> "%LOG_FILE%"
echo ============================================================= >> "%LOG_FILE%"

REM Write service_install_version to file to track successful installation
if not "!SERVICE_INSTALL_VERSION!"=="" (
    echo !SERVICE_INSTALL_VERSION!> "%SCRIPT_DIR%service_install_version_last_installed.txt"
    echo Wrote service_install_version !SERVICE_INSTALL_VERSION! to service_install_version_last_installed.txt >> "%LOG_FILE%"
)

echo.
echo =============================================================
echo   SUCCESS^^! Setup complete for !SERVICE_NAME!
echo   Environment: !ENV_NAME!
echo   The service is ready to use.
echo =============================================================
echo.
echo Next steps:
echo   1. Make sure LM Studio is running with the VieNeu GGUF model loaded
echo   2. Run RunServer.bat to start the service
echo   3. Run TestService.bat to test the running service
echo.
echo Log file: setup_log.txt
echo.
if not "%NOPAUSE%"=="nopause" (
    echo Press any key to exit...
    pause >nul
)
exit /b 0
