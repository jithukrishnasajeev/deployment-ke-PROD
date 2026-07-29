@echo off
TITLE iFlight Neo Wars - Quick Environment Setup & Launcher
COLOR 0A

echo ======================================================================
echo          iFlight Neo Wars Deployment - Automatic Setup Tool
echo ======================================================================
echo.

:: 1. Verify Python Installation
python --version >nul 2>&1
if %errorlevel% neq 0 (
    COLOR 0C
    echo [ERROR] Python is not installed or not in PATH!
    echo Please install Python 3.8+ from https://www.python.org/downloads/
    pause
    exit /b 1
)
echo [OK] Python detected.

:: 2. Install Required Python Dependencies
echo.
echo [1/4] Installing / Updating Python packages...
python -m pip install --upgrade pip >nul 2>&1
python -m pip install flask python-dotenv paramiko scp
if %errorlevel% neq 0 (
    COLOR 0E
    echo [WARNING] Some pip packages failed to install, proceeding...
)

:: 3. Configure AWS CLI Profile (.aws/config)
echo.
echo [2/4] Configuring AWS CLI SSO Profile (iFlightCrew_Dev)...
set "AWS_DIR=%USERPROFILE%\.aws"
set "AWS_CFG=%AWS_DIR%\config"

if not exist "%AWS_DIR%" (
    mkdir "%AWS_DIR%"
)

:: Check if profile already exists in config
findstr /C:"[profile iFlightCrew_Dev]" "%AWS_CFG%" >nul 2>&1
if %errorlevel% equ 0 (
    echo [OK] AWS Profile 'iFlightCrew_Dev' already configured in %AWS_CFG%.
) else (
    echo [INFO] Appending 'iFlightCrew_Dev' profile to %AWS_CFG%...
    (
        echo.
        echo [profile iFlightCrew_Dev]
        echo sso_session = ibs-sso
        echo sso_account_id = 680323250112
        echo sso_role_name = IT_DEV_AWS_Access
        echo region = ap-south-1
        echo output = json
        echo.
        echo [sso-session ibs-sso]
        echo sso_start_url = https://d-926715f2ed.awsapps.com/start
        echo sso_region = ap-south-1
    ) >> "%AWS_CFG%"
    echo [SUCCESS] AWS Profile 'iFlightCrew_Dev' added to %AWS_CFG%!
)

:: 4. Verify / Create local .env file
echo.
echo [3/4] Checking environment configuration (.env)...
if not exist ".env" (
    echo [INFO] Creating default .env file...
    (
        echo SOURCE_SERVER_PASSWORD=
        echo TARGET_SERVER_PASSWORD=
    ) > .env
    echo [OK] Created .env file. Update passwords in System Configuration tab.
) else (
    echo [OK] .env file found.
)

:: 5. Create local downloads directory
if not exist "downloads" (
    mkdir "downloads"
)

:: 6. Launch Application & Browser
echo.
echo ======================================================================
echo [4/4] Starting iFlight Neo Deployment Web Dashboard...
echo ======================================================================
echo.
echo Open browser at: http://localhost:5000
echo.

start "" "http://localhost:5000"
python web_app.py

pause
