@echo off
REM Start script for Local AI Assistant

setlocal enabledelayedexpansion

echo.
echo ===============================================
echo Local AI Assistant - Startup Script
echo ===============================================
echo.

REM Check if Ollama is installed
echo [1/4] Checking for Ollama installation...
ollama --version >nul 2>&1
if errorlevel 1 (
    echo WARNING: Ollama is not installed or not in PATH.
    echo EDITH will start, but model inference will require Ollama from https://ollama.ai
) else (
    echo [OK] Ollama found
)

REM Check if Ollama service is running
echo [2/4] Checking if Ollama service is reachable...
ollama list >nul 2>&1
if errorlevel 1 (
    echo WARNING: Ollama service is not currently running.
    echo Please start Ollama when convenient with: ollama serve
) else (
    echo [OK] Ollama is running
)

REM Check Ollama model availability without automatic downloads
echo [3/4] Verifying Ollama model availability...
ollama list 2>nul
echo Note: Ensure your configured model (set in .env as OLLAMA_MODEL) is available in Ollama.
echo EDITH will not automatically download, pull, or preload any model.

REM Check Python executable (prefer repository virtual environment if present)
if exist "%~dp0.venv\Scripts\python.exe" (
    set "PY_CMD=%~dp0.venv\Scripts\python.exe"
    echo [OK] Using virtual environment Python: !PY_CMD!
) else (
    set "PY_CMD=python"
    echo [OK] Using system Python
)

REM Start WhatsApp bridge if Node.js/npm is installed
echo.
echo [4/4] Starting messaging bridges and backend...
where npm >nul 2>&1
if errorlevel 1 (
    echo WARNING: Node.js/npm was not found in PATH.
    echo WhatsApp bridge will not be started.
    echo EDITH will operate in API and Telegram mode.
) else (
    echo Starting WhatsApp Bridge in separate window...
    cd /d "%~dp0whatsapp_bridge"
    start "Local AI - WhatsApp Bridge" cmd /k "npm start"
    timeout /t 3 /nobreak >nul 2>&1
)

REM Start Python backend
echo Starting Python Backend in separate window...
cd /d "%~dp0"
start "Local AI - Backend" cmd /k ""!PY_CMD!" main.py"

echo.
echo ===============================================
echo Services Initialization Initiated:
echo - Python Backend: http://127.0.0.1:8000
echo - API Health: GET http://127.0.0.1:8000/api/health
echo - WhatsApp Bridge: http://127.0.0.1:3000 (if npm installed)
echo - WhatsApp Pairing: http://127.0.0.1:3000/qr-display
echo ===============================================
echo.
pause
