@echo off
rem codriver -- start the browser UI. Run install.bat once first.
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Run install.bat first.
  pause
  exit /b 1
)
.venv\Scripts\python -m codriver ui %*
pause
