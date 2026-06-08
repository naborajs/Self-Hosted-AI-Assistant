@echo off
REM Restart script for Local AI Assistant

echo.
echo ===============================================
echo Local AI Assistant - Restart Script
echo ===============================================
echo.

echo Stopping services...
call "%~dp0stop.bat"

timeout /t 2 /nobreak

echo.
echo Starting services...
call "%~dp0start.bat"
