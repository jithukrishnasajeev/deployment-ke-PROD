@echo off
echo ========================================
echo iFlight Neo Wars Deployment Automation
echo ========================================
echo.

REM Check if paramiko is installed
python -c "import paramiko" 2>nul
if errorlevel 1 (
    echo Installing required dependencies...
    pip install -r requirements_deployment.txt
    echo.
)

python deployment_automation.py %*

pause
