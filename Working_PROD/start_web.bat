@echo off
echo ========================================
echo iFlight Neo Deployment - Web Interface
echo ========================================
echo.

if "%1"=="--clean" (
    echo [CLEAN INSTALL] Force reinstalling all dependencies from requirements_deployment.txt...
    python -m pip install --force-reinstall --no-cache-dir -r requirements_deployment.txt
) else if "%1"=="-clean" (
    echo [CLEAN INSTALL] Force reinstalling all dependencies from requirements_deployment.txt...
    python -m pip install --force-reinstall --no-cache-dir -r requirements_deployment.txt
) else (
    echo [INFO] Installing/verifying all required dependencies...
    python -m pip install --upgrade -r requirements_deployment.txt
)
echo.

REM Start the web application
python web_app.py

pause
