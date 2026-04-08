@echo off
echo ========================================
echo iFlight Neo Deployment - Web Interface
echo ========================================
echo.

REM Install dependencies if needed
python -m pip install -r requirements_deployment.txt --quiet

REM Start the web application
python web_app.py

pause
