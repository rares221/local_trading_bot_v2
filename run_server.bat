@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] No virtualenv at .venv
  echo Create it:  python -m venv .venv
  echo Then:       .venv\Scripts\pip install -r requirements.txt
  pause
  exit /b 1
)

echo Starting server with: .venv\Scripts\python.exe
".venv\Scripts\python.exe" -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
pause
