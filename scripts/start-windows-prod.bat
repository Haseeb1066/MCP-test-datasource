@echo off
REM Production start on Windows Server (replaces Render).
REM Prereqs: Python 3.10+, Node 18+, .env configured, then once:
REM   python -m venv .venv
REM   .venv\Scripts\activate
REM   pip install -r requirements.txt
REM   npm install
REM   npm run build

cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
  echo Missing .venv — create it first: python -m venv .venv
  exit /b 1
)

if not exist "dist\web\index.html" (
  echo UI not built — running npm run build...
  call npm run build
  if errorlevel 1 exit /b 1
)

set PORT=8787
echo Starting Tableau MCP Chat on http://0.0.0.0:%PORT%
echo Point extension\TableauMcpChat.trex url to https://YOUR_SERVER/ or http://THIS_PC:%PORT%/
.venv\Scripts\python.exe -m uvicorn backend.main:app --host 0.0.0.0 --port %PORT%
