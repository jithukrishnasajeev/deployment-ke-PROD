@echo off
echo ========================================
echo iFlight Neo Wars Deployment Automation
echo ========================================
echo.

if "%1"=="--clean" goto FORCE_INSTALL
if "%1"=="-clean" goto FORCE_INSTALL

REM Smart Check: Verify if all required Python dependencies are already installed
python -c "import paramiko, scp, flask, dotenv" 2>nul
if errorlevel 1 (
    echo [INFO] Missing dependencies detected. Installing required packages...
    python -m pip install -r requirements_deployment.txt
    echo.
) else (
    echo [INFO] All dependencies verified.
)
goto START_APP

:FORCE_INSTALL
echo [CLEAN INSTALL] Force reinstalling all dependencies...
python -m pip install --force-reinstall --no-cache-dir -r requirements_deployment.txt
shift
echo.

:START_APP
python deployment_automation.py %*

pause
