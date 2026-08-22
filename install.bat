@echo off
rem codriver -- one-time setup. Needs Python 3.11+ from python.org (tick "Add to PATH").
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install it from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" in the installer, then run this again.
  pause
  exit /b 1
)

if not exist .venv (
  echo Creating a private Python environment in .venv ...
  python -m venv .venv || (echo venv failed & pause & exit /b 1)
)

echo Installing codriver and its dependencies ...
.venv\Scripts\python -m pip install --upgrade pip >nul
.venv\Scripts\python -m pip install -e ".[voice]" || (echo install failed & pause & exit /b 1)

echo.
echo Done. Double-click start.bat to open the co-driver.
echo The first start asks the Windows firewall to allow it on private networks --
echo that is what lets your phone show the HUD.
pause
