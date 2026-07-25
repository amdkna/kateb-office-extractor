@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_CMD="
set "PYTHON_ARGS="

where py >nul 2>nul
if not errorlevel 1 (
  set "PYTHON_CMD=py"
  set "PYTHON_ARGS=-3"
)

if not defined PYTHON_CMD (
  where python >nul 2>nul
  if not errorlevel 1 set "PYTHON_CMD=python"
)

if not defined PYTHON_CMD (
  echo Python was not found.
  echo Install Python 3.11 or newer from python.org and enable "Add Python to PATH".
  pause
  exit /b 1
)

if not exist .venv (
  %PYTHON_CMD% %PYTHON_ARGS% -m venv .venv
  if errorlevel 1 goto :error
)

call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
if errorlevel 1 goto :error

pip install -r requirements.txt
if errorlevel 1 goto :error

if not exist .env copy .env.example .env >nul

echo.
echo Setup completed.
echo Edit .env, then run run.bat
echo To open the data manager without a command window, run run-office-manager.vbs
pause
exit /b 0

:error
echo.
echo Setup failed.
pause
exit /b 1
