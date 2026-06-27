@echo off
title Horon Launcher
cd /d "%~dp0"

echo Starting Horon...
echo.

start "Horon API (port 8710)" cmd /k "cd /d "%~dp0" && python -m uvicorn backend.server:app --port 8710 --reload"
start "Horon Web (Vite)" cmd /k "cd /d "%~dp0\web" && npm run dev"

timeout /t 3 /nobreak >nul
start http://localhost:5173

echo Both servers launched. Close this window anytime.
