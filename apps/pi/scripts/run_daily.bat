@echo off
chcp 65001 >nul
REM Daily Product Scout Runner for Windows Task Scheduler or manual start
setlocal enabledelayedexpansion
cd /d "%~dp0\.."
set "PI_REPO=%CD%"

REM Preflight 1: Verify Product-Intelligence Python
set "PYTHON_EXEC=%PI_REPO%\.venv\Scripts\python.exe"
if not exist "%PYTHON_EXEC%" (
    echo [PREFLIGHT_ERROR_10] Product-Intelligence virtual environment not found at %PYTHON_EXEC%
    exit /b 10
)

REM Preflight 2: Verify exchange inbox parent directory
set "EXCHANGE_INBOX=D:\work\Personal\hermes-product-video-exchange\inbox"
for %%I in ("%EXCHANGE_INBOX%\..") do set "EXCHANGE_PARENT=%%~fI"
if not exist "%EXCHANGE_PARENT%" (
    echo [PREFLIGHT_ERROR_40] Exchange inbox parent directory not found at %EXCHANGE_PARENT%
    exit /b 40
)

echo [%date% %time%] Starting Daily Product Scout...
"%PYTHON_EXEC%" scripts\daily_scout.py run
if %ERRORLEVEL% NEQ 0 (
    echo [%date% %time%] Daily Product Scout finished with error code %ERRORLEVEL%
    exit /b %ERRORLEVEL%
)

echo [%date% %time%] Daily Product Scout finished successfully.

echo [%date% %time%] Publishing daily run to exchange inbox...
"%PYTHON_EXEC%" scripts\publish_exchange.py
set "EXCHANGE_PUBLISH_EXIT=%ERRORLEVEL%"
if %EXCHANGE_PUBLISH_EXIT% NEQ 0 (
    echo [%date% %time%] Exchange publishing failed with code %EXCHANGE_PUBLISH_EXIT%
    exit /b %EXCHANGE_PUBLISH_EXIT%
)

echo [%date% %time%] Daily scout batch successfully published to exchange inbox.
echo [%date% %time%] Producer-only pipeline complete. Ready for Hermes ingestion on operator demand.
exit /b 0

