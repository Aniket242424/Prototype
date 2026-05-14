@echo off
REM ============================================================
REM Trading_Agent launcher + watchdog.
REM
REM Double-click to start the system. This launches the FastAPI app
REM which auto-starts all 3 workers via the in-process supervisor.
REM If the API exits (e.g. you click "Restart API" on the dashboard),
REM this script automatically respawns it after a 2-second pause.
REM
REM To stop the whole stack: close this console window.
REM ============================================================

setlocal
cd /d "%~dp0"
title Trading_Agent (close this window to stop)

REM Make sure docker dependencies are up before the API starts.
echo [start.bat] Checking docker compose services (postgres, redis)...
docker compose ps --status running --services 2>nul | findstr /B "postgres" >nul
if errorlevel 1 (
    echo [start.bat] Starting postgres + redis...
    docker compose up -d postgres redis
)

echo.
echo [start.bat] Launching API + workers. Open http://localhost:8000/dashboard in your browser.
echo [start.bat] Close this window to stop everything.
echo.

:loop
REM Wait until port 8000 is free before launching (avoids spawn/kill churn)
:wait_port
netstat -ano | findstr /R /C:"LISTENING.*:8000 " >nul 2>&1
if not errorlevel 1 (
    echo [start.bat] Port 8000 already in use. Waiting 3s for it to free...
    timeout /t 3 /nobreak >nul
    goto wait_port
)

py -3.14 -u -m uvicorn trading_agent.api.main:app --host 0.0.0.0 --port 8000
echo.
echo [start.bat] API exited with code %errorlevel%. Restarting in 2s... (Ctrl+C to abort)
timeout /t 2 /nobreak >nul
goto loop
