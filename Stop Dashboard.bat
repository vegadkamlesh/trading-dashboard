@echo off
REM ====================================================================
REM  Double-click this file to STOP the whole dashboard.
REM  Force-stops project backend/frontend process trees and checks ports.
REM  Works even if the original windows were closed (orphaned processes).
REM ====================================================================
title Dhan - STOP Dashboard

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Stop-All.ps1" %*
