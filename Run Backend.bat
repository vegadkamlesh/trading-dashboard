@echo off
REM ====================================================================
REM  Runs ONLY the backend (http://127.0.0.1:8000).
REM  Keep the frontend running separately if you only restart the API.
REM ====================================================================
title Dhan - Backend (API :8000)

set "ROOT=%~dp0"
set "MACHINE=%Path%"
REM Refresh PATH so node/npm/python are found without reopening a shell.
for /f "tokens=2*" %%A in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path ^| findstr /i "Path"') do set "SYS_PATH=%%B"
for /f "tokens=2*" %%A in ('reg query "HKCU\Environment" /v Path ^| findstr /i "Path"') do set "USR_PATH=%%B"
set "PATH=%SYS_PATH%;%USR_PATH%;%PATH%"

cd /d "%ROOT%backend"

if not exist ".venv" (
    echo Creating Python virtual environment...
    python -m venv .venv
    .venv\Scripts\python.exe -m pip install --upgrade pip
    .venv\Scripts\python.exe -m pip install -r requirements.txt
)

if not exist ".env" (
    echo.
    echo  WARNING: backend\.env not found.
    echo  Copy env.example.txt to .env and add your Dhan token.
    echo  For an offline demo use:  DHAN_ACCESS_TOKEN=mock-token
    echo.
    notepad env.example.txt
    pause
)

set PYTHONPATH=.
echo.
echo  Backend running at http://127.0.0.1:8000
echo  (API docs at http://127.0.0.1:8000/docs)
echo.
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

pause
