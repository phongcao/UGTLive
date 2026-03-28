@echo off
REM Force UTF-8 encoding for international compatibility
chcp 65001 >nul 2>&1
setlocal ENABLEDELAYEDEXPANSION

set "SCRIPT_DIR=%~dp0"
set "CONFIG_FILE=%SCRIPT_DIR%service_config.txt"
set "LOG_FILE=%SCRIPT_DIR%setup_log.txt"
set "VENV_DIR=%SCRIPT_DIR%venv"
set "UTIL_DIR=%SCRIPT_DIR%..\util"
set "NOPAUSE=%~1"
set "FORCE_GPU=%~2"

REM Delete existing log file
if exist "%LOG_FILE%" del "%LOG_FILE%"

REM Create log file and add header
echo ============================================================= >> "%LOG_FILE%"
echo Setup Log - %date% %time% >> "%LOG_FILE%"
echo ============================================================= >> "%LOG_FILE%"
echo. >> "%LOG_FILE%"

REM -----------------------------------------------------------------
REM Parse service_config.txt to get environment name and service name
REM Note: service_name and venv_name should use ASCII characters
REM       only for maximum compatibility across different locales.
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

if "!SERVICE_NAME!"=="" set "SERVICE_NAME=OCR Service"

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
REM Detect GPU - or use forced GPU series for testing
REM Usage: Install.bat [nopause] [30_40|50|60]
REM -----------------------------------------------------------------
if "!FORCE_GPU!"=="30_40" (
    echo FORCED GPU SERIES: 30_40 [test mode]
    echo FORCED GPU SERIES: 30_40 >> "%LOG_FILE%"
    set "GPU_SERIES=30_40"
    set "GPU_NAME=Forced RTX 30/40 series"
    goto :SkipGPUDetect
)
if "!FORCE_GPU!"=="50" (
    echo FORCED GPU SERIES: 50 [test mode]
    echo FORCED GPU SERIES: 50 >> "%LOG_FILE%"
    set "GPU_SERIES=50"
    set "GPU_NAME=Forced RTX 50 series"
    goto :SkipGPUDetect
)
if "!FORCE_GPU!"=="60" (
    echo FORCED GPU SERIES: 60 [test mode]
    echo FORCED GPU SERIES: 60 >> "%LOG_FILE%"
    set "GPU_SERIES=60"
    set "GPU_NAME=Forced RTX 60 series"
    goto :SkipGPUDetect
)

echo Detecting GPU...
echo.
echo Detecting GPU... >> "%LOG_FILE%"

set "GPU_NAME=Unknown"
set "GPU_SERIES=UNSUPPORTED"

nvidia-smi --query-gpu=name --format=csv,noheader >"%SCRIPT_DIR%gpu_check_temp.txt" 2>nul
if exist "%SCRIPT_DIR%gpu_check_temp.txt" (
    set /p GPU_NAME=<"%SCRIPT_DIR%gpu_check_temp.txt"
    del "%SCRIPT_DIR%gpu_check_temp.txt" >nul 2>&1
)

if not "!GPU_NAME!"=="Unknown" (
    echo Detected NVIDIA GPU: !GPU_NAME!
    echo GPU Detected: !GPU_NAME! >> "%LOG_FILE%"
    
    REM Check for RTX 50 series
    echo !GPU_NAME! | find /i "RTX 50" >nul && set "GPU_SERIES=50"
    
    REM Check for RTX 60 series (Blackwell consumer)
    echo !GPU_NAME! | find /i "RTX 60" >nul && set "GPU_SERIES=60"
    
    REM Check for RTX PRO series (Blackwell workstation, e.g. RTX PRO 6000)
    echo !GPU_NAME! | find /i "RTX PRO" >nul && set "GPU_SERIES=60"
    
    REM Check for RTX 2000/3000/4000 series
    if "!GPU_SERIES!"=="UNSUPPORTED" (
        echo !GPU_NAME! | find /i "RTX 20" >nul && set "GPU_SERIES=30_40"
        echo !GPU_NAME! | find /i "RTX 30" >nul && set "GPU_SERIES=30_40"
        echo !GPU_NAME! | find /i "RTX 40" >nul && set "GPU_SERIES=30_40"
    )
    echo GPU Series: !GPU_SERIES! >> "%LOG_FILE%"
) else (
    echo WARNING: Unable to detect NVIDIA GPU via nvidia-smi
    echo WARNING: GPU not detected >> "%LOG_FILE%"
    echo Attempting to continue with RTX 3000/4000 series configuration...
    set "GPU_SERIES=30_40"
    echo Defaulting to GPU Series: 30_40 >> "%LOG_FILE%"
)

:SkipGPUDetect

echo.
echo. >> "%LOG_FILE%"

REM -----------------------------------------------------------------
REM Set Python version based on GPU series
REM -----------------------------------------------------------------
set "PYTHON_VERSION=3.12"

echo Python version: !PYTHON_VERSION! >> "%LOG_FILE%"
echo GPU Series !GPU_SERIES! requires Python !PYTHON_VERSION!
echo.

REM -----------------------------------------------------------------
REM Find Python executable from system PATH
REM -----------------------------------------------------------------
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

REM Verify Python version is 3.12
for /f "delims=" %%v in ('python --version 2^>^&1') do set "PY_VER_STR=%%v"
echo Detected !PY_VER_STR! >> "%LOG_FILE%"
echo Detected !PY_VER_STR!
echo !PY_VER_STR! | find "3.12" >nul
if errorlevel 1 (
    echo.
    echo ERROR: Python 3.12 is required but found: !PY_VER_STR!
    echo ERROR: Wrong Python version: !PY_VER_STR! >> "%LOG_FILE%"
    echo Please install Python 3.12 and ensure it is the default python in your PATH.
    echo.
    pause
    exit /b 1
)
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
echo Creating venv with Python !PYTHON_VERSION!...

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

REM Set SSL certificate file for urllib (fixes certificate errors on some systems)
for /f "delims=" %%i in ('python -c "import certifi; print(certifi.where())" 2^>nul') do set "SSL_CERT_FILE=%%i"
set "SSL_CERT_DIR="

echo Environment variables set >> "%LOG_FILE%"

REM -----------------------------------------------------------------
REM Install dependencies based on GPU series
REM -----------------------------------------------------------------
echo [4/4] Installing dependencies for GPU series: !GPU_SERIES!
echo [4/4] Installing dependencies for GPU series: !GPU_SERIES! >> "%LOG_FILE%"

if "!GPU_SERIES!"=="50" (
    echo.
    echo Installing for RTX 50 series with PyTorch nightly CUDA 12.8...
    echo Installing for RTX 50 series >> "%LOG_FILE%"
    echo Calling Install50Series function... >> "%LOG_FILE%"
    call :Install50Series
    if errorlevel 1 (
        echo ERROR: Install50Series failed with error level !errorlevel! >> "%LOG_FILE%"
        pause
        exit /b 1
    )
    echo Install50Series completed successfully >> "%LOG_FILE%"
    goto :SetupComplete
)

if "!GPU_SERIES!"=="60" (
    echo.
    echo Installing for RTX 60 series with PyTorch nightly CUDA 12.8...
    echo Installing for RTX 60 series >> "%LOG_FILE%"
    echo Calling Install50Series function... >> "%LOG_FILE%"
    call :Install50Series
    if errorlevel 1 (
        echo ERROR: Install50Series failed with error level !errorlevel! >> "%LOG_FILE%"
        pause
        exit /b 1
    )
    echo Install50Series completed successfully >> "%LOG_FILE%"
    goto :SetupComplete
)

if "!GPU_SERIES!"=="30_40" (
    echo.
    echo Installing for RTX 30/40 series with PyTorch 2.6 CUDA 11.8...
    echo Installing for RTX 30/40 series >> "%LOG_FILE%"
    echo Calling Install30_40Series function... >> "%LOG_FILE%"
    call :Install30_40Series
    if errorlevel 1 (
        echo ERROR: Install30_40Series failed with error level !errorlevel! >> "%LOG_FILE%"
        pause
        exit /b 1
    )
    echo Install30_40Series completed successfully >> "%LOG_FILE%"
    goto :SetupComplete
)

REM If we get here, unsupported GPU
echo ERROR: Unsupported GPU series: !GPU_SERIES! >> "%LOG_FILE%"
goto :FailGPU

:SetupComplete

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
echo   SUCCESS! Setup complete for !SERVICE_NAME!
echo   Environment: !ENV_NAME!
echo   The service is ready to use.
echo =============================================================
echo.
echo Next steps:
echo   1. Run DiagnosticTest.bat to verify the installation
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

REM -----------------------------------------------------------------
REM Installation for RTX 30/40 Series
REM -----------------------------------------------------------------
:Install30_40Series
echo.
echo ============================================================
echo Installing dependencies for RTX 30/40 Series
echo ============================================================
echo.

set "PYTORCH_CHANNEL=https://download.pytorch.org/whl/cu118"
set "TORCH_VERSION=2.6.0+cu118"
set "TORCHVISION_VERSION=0.21.0+cu118"
set "TORCHAUDIO_VERSION=2.6.0+cu118"

echo [Step 1/6] Upgrading pip...
python -m pip install --upgrade pip setuptools wheel
if errorlevel 1 (
    echo ERROR: Failed to upgrade pip!
    pause
    exit /b 1
)

echo [Step 2/6] Installing PyTorch 2.6 GPU stack (CUDA 11.8)...
echo [Step 2/6] Installing PyTorch 2.6 (CUDA 11.8)... >> "%LOG_FILE%"
python -m pip install --extra-index-url !PYTORCH_CHANNEL! torch==!TORCH_VERSION! torchvision==!TORCHVISION_VERSION! torchaudio==!TORCHAUDIO_VERSION! >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo ERROR: Failed to install PyTorch!
    echo ERROR: Failed to install PyTorch >> "%LOG_FILE%"
    pause
    exit /b 1
)
echo PyTorch installed successfully >> "%LOG_FILE%"

echo [Step 3/6] Installing core dependencies...
echo [Step 3/6] Installing core dependencies... >> "%LOG_FILE%"
python -m pip install numpy==2.0.2 pillow==11.3.0 "opencv-python-headless>=4.10,<4.12" scipy==1.13.1 certifi >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo ERROR: Failed to install core dependencies!
    echo ERROR: Failed to install core dependencies >> "%LOG_FILE%"
    pause
    exit /b 1
)
echo Core dependencies installed successfully >> "%LOG_FILE%"

echo [Step 4/6] Installing Florence-2 dependencies (transformers, einops, timm)...
echo [Step 4/6] Installing Florence-2 dependencies... >> "%LOG_FILE%"
python -m pip install "transformers==4.46.3" einops timm >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo ERROR: Failed to install Florence-2 dependencies!
    echo ERROR: Failed to install Florence-2 dependencies >> "%LOG_FILE%"
    pause
    exit /b 1
)
echo Florence-2 dependencies installed successfully >> "%LOG_FILE%"

echo [Step 5/6] Installing FastAPI and Uvicorn...
echo [Step 5/6] Installing FastAPI/Uvicorn... >> "%LOG_FILE%"
python -m pip install fastapi==0.121.1 "uvicorn[standard]==0.38.0" python-multipart==0.0.20 pydantic==2.10.5 >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo ERROR: Failed to install FastAPI/Uvicorn!
    echo ERROR: Failed to install FastAPI/Uvicorn >> "%LOG_FILE%"
    pause
    exit /b 1
)
echo FastAPI/Uvicorn installed successfully >> "%LOG_FILE%"

echo [Step 6/6] Installing Scikit-learn (optional but useful for color analysis fallback)...
echo [Step 6/6] Installing Scikit-learn... >> "%LOG_FILE%"
python -m pip install scikit-learn >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo WARNING: Scikit-learn installation failed, some fallbacks may be limited
    echo WARNING: Scikit-learn installation failed >> "%LOG_FILE%"
) else (
    echo Scikit-learn installed successfully >> "%LOG_FILE%"
)

echo.
echo Verifying installation...
echo Verifying installation... >> "%LOG_FILE%"
python -c "import torch; print('PyTorch:', torch.__version__); print('CUDA Available:', torch.cuda.is_available())" >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo ERROR: PyTorch verification failed!
    echo ERROR: PyTorch verification failed >> "%LOG_FILE%"
    pause
    exit /b 1
)
echo PyTorch verification passed >> "%LOG_FILE%"

python -c "import transformers; print('Transformers:', transformers.__version__)" >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo ERROR: Transformers verification failed!
    echo ERROR: Transformers verification failed >> "%LOG_FILE%"
    pause
    exit /b 1
)
echo Transformers verification passed >> "%LOG_FILE%"

python -c "import fastapi; print('FastAPI imported successfully')" >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo ERROR: FastAPI verification failed!
    echo ERROR: FastAPI verification failed >> "%LOG_FILE%"
    pause
    exit /b 1
)
echo FastAPI verification passed >> "%LOG_FILE%"

goto :eof

REM -----------------------------------------------------------------
REM Installation for RTX 50 Series (PyTorch Nightly with CUDA 12.8)
REM -----------------------------------------------------------------
:Install50Series
echo.
echo ============================================================
echo Installing dependencies for RTX 50 Series
echo ============================================================
echo.

echo [Step 1/7] Upgrading pip and installing core dependencies first...
echo [Step 1/7] Upgrading pip and core deps... >> "%LOG_FILE%"
python -m pip install --upgrade pip setuptools wheel >> "%LOG_FILE%" 2>&1
python -m pip install numpy==2.1.3 pillow opencv-python scipy tqdm pyyaml requests certifi >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo ERROR: Failed to install core dependencies!
    echo ERROR: Failed to install core dependencies >> "%LOG_FILE%"
    pause
    exit /b 1
)
echo Core dependencies installed successfully >> "%LOG_FILE%"

echo [Step 2/7] Installing PyTorch nightly with CUDA 12.8 support...
echo This may take several minutes...
echo [Step 2/7] Installing PyTorch nightly (CUDA 12.8)... >> "%LOG_FILE%"
python -m pip install --pre torch torchvision torchaudio --index-url https://download.pytorch.org/whl/nightly/cu128 >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo ERROR: Failed to install PyTorch nightly!
    echo ERROR: Failed to install PyTorch nightly >> "%LOG_FILE%"
    pause
    exit /b 1
)
echo PyTorch nightly installed successfully >> "%LOG_FILE%"

echo [Step 3/7] Installing Florence-2 dependencies...
echo [Step 3/7] Installing Florence-2 dependencies... >> "%LOG_FILE%"
python -m pip install "transformers==4.46.3" einops timm >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo ERROR: Failed to install Florence-2 dependencies!
    echo ERROR: Failed to install Florence-2 dependencies >> "%LOG_FILE%"
    pause
    exit /b 1
)
echo Florence-2 dependencies installed successfully >> "%LOG_FILE%"

echo [Step 4/7] Reinstalling PyTorch nightly to ensure correct version...
echo [Step 4/7] Reinstalling PyTorch nightly... >> "%LOG_FILE%"
python -m pip install --pre torch torchvision torchaudio --index-url https://download.pytorch.org/whl/nightly/cu128 --force-reinstall --no-deps >> "%LOG_FILE%" 2>&1
echo PyTorch nightly reinstalled >> "%LOG_FILE%"

echo [Step 5/7] Installing FastAPI and Uvicorn...
echo [Step 5/7] Installing FastAPI/Uvicorn... >> "%LOG_FILE%"
python -m pip install fastapi "uvicorn[standard]" python-multipart >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo ERROR: Failed to install FastAPI/Uvicorn!
    echo ERROR: Failed to install FastAPI/Uvicorn >> "%LOG_FILE%"
    pause
    exit /b 1
)
echo FastAPI/Uvicorn installed successfully >> "%LOG_FILE%"

echo [Step 6/7] Installing Scikit-learn (optional but useful for color analysis fallback)...
echo [Step 6/7] Installing Scikit-learn... >> "%LOG_FILE%"
python -m pip install scikit-learn >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo WARNING: Scikit-learn installation failed, some fallbacks may be limited
    echo WARNING: Scikit-learn installation failed >> "%LOG_FILE%"
) else (
    echo Scikit-learn installed successfully >> "%LOG_FILE%"
)

echo [Step 7/7] Pre-downloading Florence-2 model...
echo This may take several minutes (model is ~1.5GB)...
echo [Step 7/7] Pre-downloading Florence-2 model... >> "%LOG_FILE%"
REM Set SSL cert for model downloads (certifi is now installed)
for /f "delims=" %%i in ('python -c "import certifi; print(certifi.where())"') do set "SSL_CERT_FILE=%%i"
python -c "import ssl, certifi; ssl._create_default_https_context = lambda: ssl.create_default_context(cafile=certifi.where()); from transformers import AutoProcessor, AutoModelForCausalLM; AutoProcessor.from_pretrained('microsoft/Florence-2-large', trust_remote_code=True); AutoModelForCausalLM.from_pretrained('microsoft/Florence-2-large', trust_remote_code=True); print('Florence-2 model downloaded successfully')" >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo WARNING: Failed to pre-download Florence-2 model
    echo WARNING: Failed to pre-download model >> "%LOG_FILE%"
) else (
    echo Model downloaded successfully >> "%LOG_FILE%"
)

echo.
echo Verifying installation...
echo Verifying installation... >> "%LOG_FILE%"
python -c "import torch; print('PyTorch:', torch.__version__); print('CUDA Version:', torch.version.cuda); print('CUDA Available:', torch.cuda.is_available()); print('GPU Count:', torch.cuda.device_count() if torch.cuda.is_available() else 0)" >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo ERROR: PyTorch verification failed!
    echo ERROR: PyTorch verification failed >> "%LOG_FILE%"
    pause
    exit /b 1
)
echo PyTorch verification passed >> "%LOG_FILE%"

python -c "import transformers; print('Transformers:', transformers.__version__)" >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo ERROR: Transformers verification failed!
    echo ERROR: Transformers verification failed >> "%LOG_FILE%"
    pause
    exit /b 1
)
echo Transformers verification passed >> "%LOG_FILE%"

python -c "import fastapi; print('FastAPI imported successfully')" >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo ERROR: FastAPI verification failed!
    echo ERROR: FastAPI verification failed >> "%LOG_FILE%"
    pause
    exit /b 1
)
echo FastAPI verification passed >> "%LOG_FILE%"

goto :eof

REM -----------------------------------------------------------------
REM Error handler for unsupported GPU
REM -----------------------------------------------------------------
:FailGPU
echo. >> "%LOG_FILE%"
echo ERROR: Unsupported GPU series >> "%LOG_FILE%"
echo.
echo ===== ERROR: Unsupported GPU =====
echo.
echo This setup currently supports:
echo   - NVIDIA RTX 3050, 3060, 3070, 3080, 3090
echo   - NVIDIA RTX 4060, 4070, 4080, 4090
echo   - NVIDIA RTX 5050, 5060, 5070, 5080, 5090
echo   - NVIDIA RTX 6070, 6080, 6090
echo.
echo If you have one of these GPUs but it wasn't detected correctly,
echo please ensure your NVIDIA drivers are installed and up to date.
echo.
echo Check setup_log.txt for details.
echo.
if not "%NOPAUSE%"=="nopause" pause
exit /b 1
