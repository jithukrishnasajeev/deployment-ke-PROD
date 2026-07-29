@echo off
echo ========================================
echo iFlight Neo Wars Deployment Automation
echo ========================================
echo.

if "%1"=="--clean" (
    echo [CLEAN INSTALL] Force reinstalling all dependencies from requirements_deployment.txt...
    python -m pip install --force-reinstall --no-cache-dir -r requirements_deployment.txt
    shift
) else if "%1"=="-clean" (
    echo [CLEAN INSTALL] Force reinstalling all dependencies from requirements_deployment.txt...
    python -m pip install --force-reinstall --no-cache-dir -r requirements_deployment.txt
    shift
) else (
    echo [INFO] Installing/verifying all required dependencies...
    python -m pip install --upgrade -r requirements_deployment.txt
)
echo.

python deployment_automation.py %*

pause
