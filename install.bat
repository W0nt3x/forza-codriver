@echo off
rem codriver -- one-time setup.
rem Finds Python (or offers to install it), creates a private environment in
rem .venv, and installs codriver into it. Safe to run again.
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "PY="

rem ---- 1. Is a suitable Python already here? ---------------------------------
rem Prefer the "py" launcher (knows every installed version), then PATH.
where py >nul 2>nul && (
  for /f "delims=" %%i in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%i"
)
if not defined PY (
  where python >nul 2>nul && (
    for /f "delims=" %%i in ('python -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%i"
  )
)
if defined PY (
  "!PY!" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul || set "PY="
)

rem ---- 2. No Python: offer to install it -------------------------------------
if not defined PY (
  echo.
  echo Python 3.11 or newer was not found on this PC.
  echo.
  where winget >nul 2>nul
  if errorlevel 1 (
    echo Please install it from https://www.python.org/downloads/ and tick
    echo "Add python.exe to PATH" in the installer. Then run this again.
    pause
    exit /b 1
  )
  set "ANSWER=Y"
  set /p "ANSWER=Install Python 3.12 now with winget? [Y/n] (recommended) "
  if /i "!ANSWER!"=="n" (
    echo.
    echo Fine. Install it yourself from https://www.python.org/downloads/
    echo ^(tick "Add python.exe to PATH"^), then run this again.
    pause
    exit /b 1
  )
  echo Installing Python 3.12 ...
  winget install --id Python.Python.3.12 -e --scope user --accept-source-agreements --accept-package-agreements --silent
  rem The new PATH is not visible in this window yet, so look where winget put it.
  for %%d in ("%LOCALAPPDATA%\Programs\Python\Python312" "%LOCALAPPDATA%\Programs\Python\Python313" "%ProgramFiles%\Python312" "%ProgramFiles%\Python313") do (
    if not defined PY if exist "%%~d\python.exe" set "PY=%%~d\python.exe"
  )
  if not defined PY (
    echo Python was installed but this window cannot see it yet.
    echo Close this window and run install.bat once more.
    pause
    exit /b 1
  )
)

echo Using Python: !PY!

rem ---- 3. Private environment + install -------------------------------------
if not exist .venv (
  echo Creating a private Python environment in .venv ...
  "!PY!" -m venv .venv || (echo venv failed & pause & exit /b 1)
)

echo Installing codriver and its dependencies ^(a minute or two^) ...
.venv\Scripts\python -m pip install --upgrade pip >nul
.venv\Scripts\python -m pip install -e ".[voice]" || (echo install failed & pause & exit /b 1)

echo.
echo Done. Double-click start.bat to open the co-driver.
echo The first start asks the Windows firewall to allow it on private networks.
echo That is what lets your phone show the HUD; say yes.
echo Later: update.bat fetches the newest version.
pause
