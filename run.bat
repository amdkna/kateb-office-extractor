@echo off
setlocal
cd /d "%~dp0"

if not exist .venv\Scripts\python.exe (
  echo The virtual environment is missing. Run setup.bat first.
  pause
  exit /b 1
)

if not exist .env (
  copy .env.example .env >nul
  echo Created .env from .env.example. Edit it before continuing.
  pause
)

.venv\Scripts\python.exe extractor.py
set EXIT_CODE=%ERRORLEVEL%

echo.
if "%EXIT_CODE%"=="0" (
  echo Finished. Results are in data\office_data.sqlite3.
) else (
  echo The program exited with code %EXIT_CODE%.
)
pause
exit /b %EXIT_CODE%
