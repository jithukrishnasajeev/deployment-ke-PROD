@echo off
echo ========================================
echo iFlight Neo Wars Deployment Automation
echo ========================================
echo.

if "%1"=="--clean" (
    echo [CLEAN INSTALL] Force reinstalling all dependencies from requirements.txt...
    python -m pip install --force-reinstall --no-cache-dir -r requirements.txt
    shift
) else if "%1"=="-clean" (
    echo [CLEAN INSTALL] Force reinstalling all dependencies from requirements.txt...
    python -m pip install --force-reinstall --no-cache-dir -r requirements.txt
    shift
) else (
    echo [INFO] Installing/verifying all required dependencies...
    python -m pip install --upgrade -r requirements.txt
)
echo.

python deployment_automation.py %*

pause
