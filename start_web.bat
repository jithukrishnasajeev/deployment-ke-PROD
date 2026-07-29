@echo off
echo ========================================
echo iFlight Neo Deployment - Web Interface
echo ========================================
echo.

if "%1"=="--clean" (
    echo [CLEAN INSTALL] Force reinstalling all dependencies from requirements.txt...
    python -m pip install --force-reinstall --no-cache-dir -r requirements.txt
) else if "%1"=="-clean" (
    echo [CLEAN INSTALL] Force reinstalling all dependencies from requirements.txt...
    python -m pip install --force-reinstall --no-cache-dir -r requirements.txt
) else (
    echo [INFO] Installing/verifying all required dependencies...
    python -m pip install --upgrade -r requirements.txt
)
echo.

REM Start the web application
python web_app.py

pause
