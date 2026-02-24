@echo off
title English to Malayalam PDF Translator

echo.
echo  ============================================================
echo   English to Malayalam PDF Translator
echo  ============================================================
echo.

:: Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo  ERROR: Python is not installed or not on your PATH.
    echo.
    echo  Please download and install Python from:
    echo    https://www.python.org/downloads/
    echo.
    echo  Make sure to check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

:: Install dependencies (only if not already installed)
echo  Installing required packages (first run may take a minute)...
pip install -q -r requirements.txt
if errorlevel 1 (
    echo.
    echo  ERROR: Could not install packages.
    echo  Please make sure you have an internet connection and try again.
    echo.
    pause
    exit /b 1
)

echo.
echo  Starting the translator app...
echo.
echo  ============================================================
echo   Open your web browser and go to:
echo.
echo       http://localhost:5000
echo.
echo   Keep this window open while you are using the translator.
echo   To stop the app, close this window.
echo  ============================================================
echo.

python app.py

pause
