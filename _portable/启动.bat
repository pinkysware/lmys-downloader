@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo    lmys-downloader  -  source launcher
echo ============================================
echo.

where python >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python not found.
  echo         Install Python 3.9+ first:  https://www.python.org/downloads/
  echo         During install, CHECK "Add Python to PATH".
  echo.
  echo   Alternative: just double-click lmys-downloader.exe instead
  echo   (it does not need Python).
  echo.
  pause
  exit /b 1
)

cd src

python -c "import flask, requests, Crypto" >nul 2>&1
if errorlevel 1 (
  echo First run detected: installing dependencies ...
  python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo [ERROR] Failed to install dependencies. Check your network.
    pause
    exit /b 1
  )
  echo Dependencies installed.
  echo.
)

echo Starting server. Browser will open automatically.
echo Keep this window open; close it to stop the service.
echo.
python lmys_web.py

echo.
echo Server stopped.
pause
