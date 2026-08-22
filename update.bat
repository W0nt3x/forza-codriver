@echo off
rem codriver -- update to the latest version. Your settings, stages,
rem recordings and voices are kept; only the program changes.
setlocal
cd /d "%~dp0"

if not exist .venv\Scripts\python.exe (
  echo Run install.bat first.
  pause
  exit /b 1
)

if not exist .git (
  echo This copy was not downloaded with git, so it cannot update itself.
  echo Download the latest ZIP from the project page, unzip it next to this
  echo folder, and copy over: config\local.yaml, stages\, recordings\, voices\.
  echo Or install git once and clone the project; then this script works.
  pause
  exit /b 1
)

where git >nul 2>nul
if errorlevel 1 (
  echo git was not found. Install it from https://git-scm.com/download/win
  echo or run:  winget install --id Git.Git -e
  pause
  exit /b 1
)

echo Fetching the latest version ...
git pull --ff-only || (
  echo.
  echo The update could not be applied automatically. Most likely you edited
  echo a file that also changed upstream. Run "git status" to see which.
  pause
  exit /b 1
)

echo Updating dependencies ...
.venv\Scripts\python -m pip install -q -e ".[voice]" || (echo install failed & pause & exit /b 1)

echo.
.venv\Scripts\python -c "import codriver; print('codriver', codriver.__version__, 'is ready. Start it with start.bat.')"
pause
