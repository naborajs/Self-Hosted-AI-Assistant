@echo off
REM Stop script for Local AI Assistant

echo.
echo ===============================================
echo Local AI Assistant - Stop Script
echo ===============================================
echo.

echo Stopping Python Backend...
taskkill /F /FI "WINDOWTITLE eq Local AI - Backend*" /T >nul 2>&1
taskkill /F /IM python.exe /FI "WINDOWTITLE eq Local AI - Backend*" >nul 2>&1

echo Stopping WhatsApp Bridge...
taskkill /F /FI "WINDOWTITLE eq Local AI - WhatsApp Bridge*" /T >nul 2>&1
taskkill /F /IM node.exe /FI "WINDOWTITLE eq Local AI - WhatsApp Bridge*" >nul 2>&1

echo.
echo ===============================================
echo All services stopped cleanly.
echo ===============================================
echo.
pause
