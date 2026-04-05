@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

REM ---------------------------------------------------------------------------
REM  Windows 10 / 11 (both use kernel major version 10)
REM ---------------------------------------------------------------------------
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$os = Get-CimInstance Win32_OperatingSystem -ErrorAction SilentlyContinue; ^
   if ($null -eq $os) { exit 2 }; ^
   $v = [version]$os.Version; if ($v.Major -ge 10) { exit 0 } else { exit 1 }"
set "WINCHK=!errorlevel!"
if "!WINCHK!"=="2" (
  echo [WARN] Could not read Windows version. Continuing anyway.
) else if "!WINCHK!"=="1" (
  echo [ERROR] This script expects Windows 10 or 11.
  echo         Older Windows may lack winget or current Python builds.
  pause
  exit /b 1
)

REM ---------------------------------------------------------------------------
REM  Resolve Python executable (python, py -3, or after winget)
REM ---------------------------------------------------------------------------
call :ResolvePythonExe
if errorlevel 1 (
  pause
  exit /b 1
)

REM ---------------------------------------------------------------------------
REM  Virtual environment
REM ---------------------------------------------------------------------------
if not exist ".venv\Scripts\python.exe" (
  echo [INFO] Creating virtual environment at .venv ...
  "!PYTHON_EXE!" -m venv .venv
  if errorlevel 1 (
    echo [ERROR] Failed to create .venv
    pause
    exit /b 1
  )
)
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] No virtualenv at .venv\Scripts\python.exe
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -c "import uvicorn" 2>nul
if errorlevel 1 (
  echo [INFO] Installing dependencies ^(requirements.txt^) ...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\pip.exe" install -r requirements.txt
  if errorlevel 1 (
    echo [ERROR] pip install failed
    pause
    exit /b 1
  )
)

echo Starting server with: .venv\Scripts\python.exe
".venv\Scripts\python.exe" -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
pause
goto :EOF

REM ---------------------------------------------------------------------------
REM  :ResolvePythonExe  - sets PYTHON_EXE; may install Python via winget
REM ---------------------------------------------------------------------------
:ResolvePythonExe
set "PYTHON_EXE="

for /f "delims=" %%i in ('python -c "import sys; print(sys.executable)" 2^>nul') do set "PYTHON_EXE=%%i"
if defined PYTHON_EXE if exist "!PYTHON_EXE!" goto :HavePython

for /f "delims=" %%i in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set "PYTHON_EXE=%%i"
if defined PYTHON_EXE if exist "!PYTHON_EXE!" goto :HavePython

echo [INFO] Python was not found on PATH.

where winget >nul 2>&1
if errorlevel 1 (
  echo [ERROR] winget is not available ^(common on Windows 10 before updates^).
  echo         Install Python 3 from: https://www.python.org/downloads/
  echo         Check "Add python.exe to PATH", then run this script again.
  exit /b 1
)

echo [INFO] Installing Python 3.12 for current user via winget ^(may take a minute^) ...
winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
if errorlevel 1 (
  echo [ERROR] winget install failed. Try: winget install Python.Python.3.12
  exit /b 1
)

call :RefreshPathFromRegistry

set "PYTHON_EXE="
for /f "delims=" %%i in ('python -c "import sys; print(sys.executable)" 2^>nul') do set "PYTHON_EXE=%%i"
if defined PYTHON_EXE if exist "!PYTHON_EXE!" goto :HavePython

for /f "delims=" %%i in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set "PYTHON_EXE=%%i"
if defined PYTHON_EXE if exist "!PYTHON_EXE!" goto :HavePython

REM Last resort: typical per-user install path from python.org / winget
set "PY_SCAN="
for /f "delims=" %%d in ('dir /b /ad "%LocalAppData%\Programs\Python" 2^>nul ^| sort /r') do (
  if not defined PY_SCAN if exist "%LocalAppData%\Programs\Python\%%d\python.exe" (
    set "PYTHON_EXE=%LocalAppData%\Programs\Python\%%d\python.exe"
    set "PY_SCAN=1"
  )
)
if defined PY_SCAN goto :HavePython

echo [ERROR] Python was installed but is not on PATH in this window.
echo         Close this window, open a new terminal, and run run_server.bat again.
exit /b 1

:HavePython
echo [OK] Using Python: !PYTHON_EXE!
exit /b 0

REM ---------------------------------------------------------------------------
REM  :RefreshPathFromRegistry - merge Machine + User PATH into current session
REM ---------------------------------------------------------------------------
:RefreshPathFromRegistry
for /f "usebackq delims=" %%p in (`powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "[Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')"`) do set "PATH=%%p"
exit /b 0
