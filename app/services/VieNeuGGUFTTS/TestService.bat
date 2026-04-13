@echo off
REM Force UTF-8 encoding for international compatibility
chcp 65001 >nul 2>&1
setlocal ENABLEDELAYEDEXPANSION

set "SCRIPT_DIR=%~dp0"
set "CONFIG_FILE=%SCRIPT_DIR%service_config.txt"

REM -----------------------------------------------------------------
REM Parse service_config.txt to get port and service name
REM -----------------------------------------------------------------
set "PORT="
set "SERVICE_NAME="

if exist "%CONFIG_FILE%" (
    for /f "usebackq tokens=1,2 delims=| eol=#" %%a in ("%CONFIG_FILE%") do (
        set "KEY=%%a"
        set "VALUE=%%b"
        
        REM Trim leading/trailing spaces
        for /f "tokens=*" %%x in ("!KEY!") do set "KEY=%%x"
        for /f "tokens=*" %%y in ("!VALUE!") do set "VALUE=%%y"
        
        if "!KEY!"=="port" set "PORT=!VALUE!"
        if "!KEY!"=="service_name" set "SERVICE_NAME=!VALUE!"
    )
)

if "!PORT!"=="" (
    echo ERROR: Could not find port in service_config.txt
    pause
    exit /b 1
)

if "!SERVICE_NAME!"=="" set "SERVICE_NAME=VieNeu-GGUF-TTS"

echo =============================================================
echo   Testing !SERVICE_NAME!
echo   Port: !PORT!
echo =============================================================
echo.

REM -----------------------------------------------------------------
REM Check if curl is available
REM -----------------------------------------------------------------
where curl >nul 2>nul
if errorlevel 1 (
    echo ERROR: curl is not available
    echo.
    echo curl is required for testing the service.
    echo Please ensure curl is installed and in your PATH.
    echo.
    pause
    exit /b 1
)

REM -----------------------------------------------------------------
REM Test service endpoints
REM -----------------------------------------------------------------
echo [1/3] Testing info endpoint...
curl -s http://localhost:!PORT!/info
if errorlevel 1 (
    echo   [FAIL] Could not connect to service on port !PORT!
    echo.
    echo   Please ensure the service is running using RunServer.bat
    echo.
    pause
    exit /b 1
)
echo.
echo   [PASS] Info endpoint successful
echo.

echo [2/3] Testing voices endpoint...
curl -s http://localhost:!PORT!/voices
if errorlevel 1 (
    echo   [FAIL] Voices request failed
) else (
    echo.
    echo   [PASS] Voices endpoint successful
)
echo.

echo [3/3] Testing TTS endpoint...
echo   Sending request: text=Xin chao ban
curl -s "http://localhost:!PORT!/stream?text=Xin+chao+ban" -o "%SCRIPT_DIR%test_output.wav" -w "    HTTP Status: %%{http_code}\n    Time: %%{time_total}s\n"
if errorlevel 1 (
    echo   [FAIL] TTS request failed
    echo.
    pause
    exit /b 1
)

if exist "%SCRIPT_DIR%test_output.wav" (
    for %%F in ("%SCRIPT_DIR%test_output.wav") do set "FSIZE=%%~zF"
    echo   Output file size: !FSIZE! bytes
    if !FSIZE! GTR 1000 (
        echo   [PASS] TTS generated audio successfully
    ) else (
        echo   [WARN] Output file seems too small
    )
    del "%SCRIPT_DIR%test_output.wav" >nul 2>&1
) else (
    echo   [FAIL] No output file was created
)

echo.
echo =============================================================
echo   All tests completed!
echo =============================================================
echo.
pause
