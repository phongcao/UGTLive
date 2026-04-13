@echo off
REM Force UTF-8 encoding for international compatibility
chcp 65001 >nul 2>&1
setlocal ENABLEDELAYEDEXPANSION

set "SCRIPT_DIR=%~dp0"
set "CONFIG_FILE=%SCRIPT_DIR%service_config.txt"
set "VENV_DIR=%SCRIPT_DIR%venv"
set "NOPAUSE=%~1"

REM -----------------------------------------------------------------
REM Parse service_config.txt to get environment name
REM -----------------------------------------------------------------
set "ENV_NAME="
set "SERVICE_NAME="
set "LM_STUDIO_URL="

if exist "%CONFIG_FILE%" (
    for /f "usebackq tokens=1,2 delims=| eol=#" %%a in ("%CONFIG_FILE%") do (
        set "KEY=%%a"
        set "VALUE=%%b"
        
        REM Trim leading/trailing spaces
        for /f "tokens=*" %%x in ("!KEY!") do set "KEY=%%x"
        for /f "tokens=*" %%y in ("!VALUE!") do set "VALUE=%%y"
        
        if "!KEY!"=="venv_name" set "ENV_NAME=!VALUE!"
        if "!KEY!"=="service_name" set "SERVICE_NAME=!VALUE!"
        if "!KEY!"=="lm_studio_url" set "LM_STUDIO_URL=!VALUE!"
    )
)

if "!ENV_NAME!"=="" (
    echo ERROR: Could not find venv_name in service_config.txt
    if not "%NOPAUSE%"=="nopause" pause
    exit /b 1
)

if "!SERVICE_NAME!"=="" set "SERVICE_NAME=VieNeu-GGUF-TTS"
if "!LM_STUDIO_URL!"=="" set "LM_STUDIO_URL=http://127.0.0.1:1234"

echo =============================================================
echo   !SERVICE_NAME! Diagnostic Test
echo   Environment: !ENV_NAME!
echo   LM Studio: !LM_STUDIO_URL!
echo =============================================================
echo.

REM -----------------------------------------------------------------
REM Check if virtual environment exists
REM -----------------------------------------------------------------
echo [1/6] Checking if virtual environment exists...
if not exist "%VENV_DIR%\Scripts\activate.bat" (
    echo   [FAIL] Virtual environment does not exist at: %VENV_DIR%
    echo.
    echo   Click Install to fix this. Manual option: run Install.bat
    echo.
    if not "%NOPAUSE%"=="nopause" pause
    exit /b 1
)
echo   [PASS] Virtual environment exists
echo.

REM -----------------------------------------------------------------
REM Activate environment and test imports
REM -----------------------------------------------------------------
echo [2/6] Activating virtual environment...
call "%VENV_DIR%\Scripts\activate.bat"
if errorlevel 1 (
    echo   [FAIL] Could not activate virtual environment
    echo.
    if not "%NOPAUSE%"=="nopause" pause
    exit /b 1
)
echo   [PASS] Virtual environment activated
echo.

echo Checking Python version...
python --version
echo.

echo [3/6] Testing Python imports and library versions...

echo   - Testing vieneu SDK...
python -c "from vieneu import Vieneu; print('    vieneu SDK imported successfully')" 2>nul
if errorlevel 1 (
    echo   [FAIL] vieneu SDK import failed
    goto :TestFail
)

echo   - Testing transformers (tokenizer)...
python -c "from transformers import AutoTokenizer; print('    transformers imported successfully')" 2>nul
if errorlevel 1 (
    echo   [FAIL] transformers import failed
    goto :TestFail
)

echo   - Testing httpx (LM Studio client)...
python -c "import httpx; print('    httpx version:', httpx.__version__)" 2>nul
if errorlevel 1 (
    echo   [FAIL] httpx import failed
    goto :TestFail
)

echo   - Testing numpy...
python -c "import numpy; print('    numpy version:', numpy.__version__)" 2>nul
if errorlevel 1 (
    echo   [FAIL] numpy import failed
    goto :TestFail
)

echo   - Testing FastAPI...
python -c "import fastapi; import pydantic; print('    FastAPI version:', fastapi.__version__); print('    Pydantic version:', pydantic.__version__)" 2>nul
if errorlevel 1 (
    echo   [FAIL] FastAPI import failed
    goto :TestFail
)

echo   - Testing Uvicorn...
python -c "import uvicorn; print('    Uvicorn version:', uvicorn.__version__)" 2>nul
if errorlevel 1 (
    echo   [FAIL] Uvicorn import failed
    goto :TestFail
)

echo   [PASS] All imports successful
echo.

echo [4/6] Checking LM Studio connectivity at !LM_STUDIO_URL!...
curl -s "!LM_STUDIO_URL!/v1/models" >nul 2>&1
if errorlevel 1 (
    echo   [WARN] Could not reach LM Studio at !LM_STUDIO_URL!
    echo   Make sure LM Studio is running with the VieNeu GGUF model loaded.
) else (
    echo   [PASS] LM Studio is reachable
    curl -s "!LM_STUDIO_URL!/v1/models"
    echo.
)
echo.

echo [5/6] Checking available preset voices...
python -c "from vieneu import Vieneu; tts = Vieneu(); voices = tts.list_preset_voices(); print('    Found', len(voices), 'preset voices:'); [print('      -', d, '(' + v + ')') for d, v in voices]; tts.close()" 2>nul
if errorlevel 1 (
    echo   [WARN] Could not list preset voices (model may need to download on first use)
) else (
    echo   [PASS] Preset voices queried successfully
)
echo.

echo [6/6] Verifying server.py can be parsed and imported...
python -c "import py_compile; py_compile.compile(r'%SCRIPT_DIR%server.py', doraise=True); print('    Syntax check passed')" 2>&1
if errorlevel 1 (
    echo   [FAIL] server.py has syntax errors - the server will not start!
    goto :TestFail
)
echo   [PASS] server.py syntax is valid
echo.

echo =============================================================
echo   All diagnostic tests passed!
echo   !SERVICE_NAME! is ready to use.
echo =============================================================
echo.
if not "%NOPAUSE%"=="nopause" pause
goto :eof

:TestFail
echo.
echo =============================================================
echo   Diagnostic tests FAILED!
echo   Click Install to fix this ^(or manually run Install.bat^)
echo =============================================================
echo.
if not "%NOPAUSE%"=="nopause" pause
exit /b 1
