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
    echo ERROR: Ollama is not installed or not in PATH
    echo Please install Ollama from https://ollama.ai
    echo And add it to your system PATH
    pause
    exit /b 1
)
echo [OK] Ollama found

REM Check if Ollama service is running
echo [2/4] Checking if Ollama is running...
ollama list >nul 2>&1
if errorlevel 1 (
    echo WARNING: Ollama service is not running
    echo Please start Ollama first: ollama serve
    echo.
    pause
    exit /b 1
)
echo [OK] Ollama is running

REM Check Ollama model availability without automatic downloads
echo [3/4] Verifying Ollama model availability...
echo Available models on your system:
ollama list
echo Note: Ensure your configured model (set in .env as OLLAMA_MODEL) is available in Ollama.
echo EDITH will not automatically download, pull, or preload any model.

REM Start WhatsApp bridge
echo [4/4] Starting WhatsApp Bridge...
cd /d "%~dp0whatsapp_bridge"
start "Local AI - WhatsApp Bridge" cmd /k "npm start"
timeout /t 3 /nobreak

REM Start Python backend
echo.
echo Starting Python Backend...
cd /d "%~dp0"
start "Local AI - Backend" cmd /k "python main.py"

echo.
echo ===============================================
echo Services Started:
echo - WhatsApp Bridge: http://127.0.0.1:3000
echo - Python Backend: http://127.0.0.1:8000
echo - API Health: GET http://127.0.0.1:8000/api/health
echo ===============================================
echo.
pause
