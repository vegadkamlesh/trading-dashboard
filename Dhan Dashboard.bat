@echo off
REM ====================================================================
REM  Double-click this file to run the whole dashboard.
REM  It starts the backend + frontend and opens your browser.
REM ====================================================================
title Dhan Options Dashboard Launcher

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-All.ps1"

if errorlevel 1 (
    echo.
    echo Something went wrong. Read the messages above.
    pause
)
