@echo off
REM ====================================================================
REM  Runs ONLY the frontend (http://127.0.0.1:5173) and opens the browser.
REM  Make sure the backend is already running (use "Run Backend.bat").
REM ====================================================================
title Dhan - Frontend (UI :5173)

set "ROOT=%~dp0"
set "MACHINE=%Path%"
for /f "tokens=2*" %%A in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path ^| findstr /i "Path"') do set "SYS_PATH=%%B"
for /f "tokens=2*" %%A in ('reg query "HKCU\Environment" /v Path ^| findstr /i "Path"') do set "USR_PATH=%%B"
set "PATH=%SYS_PATH%;%USR_PATH%;%PATH%"

cd /d "%ROOT%frontend"

if not exist "node_modules" (
    echo Installing frontend dependencies ^(first run^)...
    call npm install
)

echo.
echo  Frontend running at http://127.0.0.1:5173
echo  Opening browser...
echo.

REM Open the browser a few seconds after Vite starts.
start "" cmd /c "timeout /t 5 >nul & start http://127.0.0.1:5173"

call npm run dev

pause
