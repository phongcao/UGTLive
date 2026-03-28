@echo off
chcp 65001 >nul 2>&1
setlocal ENABLEDELAYEDEXPANSION

set "SCRIPT_DIR=%~dp0"
set "CONFIG_FILE=%SCRIPT_DIR%service_config.txt"
set "TEST_IMAGE=%SCRIPT_DIR%..\shared\test_images\anime_test.jpg"
set "PORT="

if exist "%CONFIG_FILE%" (
    for /f "usebackq tokens=1,2 delims=| eol=#" %%a in ("%CONFIG_FILE%") do (
        set "KEY=%%a"
        set "VALUE=%%b"
        for /f "tokens=*" %%x in ("!KEY!") do set "KEY=%%x"
        for /f "tokens=*" %%y in ("!VALUE!") do set "VALUE=%%y"
        if "!KEY!"=="port" set "PORT=!VALUE!"
    )
)

if "!PORT!"=="" set "PORT=5005"

curl -s http://localhost:!PORT!/info
echo.
curl -s -X POST http://localhost:!PORT!/process?lang=japan -H "Content-Type: application/octet-stream" --data-binary "@%TEST_IMAGE%"
echo.
if not "%1"=="nopause" pause
exit /b 0