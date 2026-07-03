@echo off
REM ===================================================================
REM  Opportunity Monitor - double-click launcher (Windows)
REM  Runs the full Python app: jobs, programs, leadership, AND research.
REM  No API key required. A browser tab opens automatically.
REM ===================================================================
cd /d "%~dp0"

REM Find Python (prefer 'python', fall back to the 'py' launcher).
set PY=python
where python >nul 2>nul || set PY=py
where %PY% >nul 2>nul || (
  echo.
  echo Python 3.11+ was not found. Install it from https://python.org
  echo (check "Add Python to PATH" during install^), then double-click this file again.
  echo.
  pause
  exit /b 1
)

REM Optional: an API key ONLY enables the extra "Auto-generate" button.
REM Everything else works without it. To use Auto-generate, create a file
REM named apikey.txt next to this launcher containing just your key.
if exist "%~dp0apikey.txt" set /p ANTHROPIC_API_KEY=<"%~dp0apikey.txt"

REM Make sure the one dependency is present.
%PY% -c "import requests" 2>nul || %PY% -m pip install --quiet requests

echo.
echo Starting Opportunity Monitor... your browser will open at http://127.0.0.1:8765
echo Close this window (or press Ctrl+C) to stop it.
echo.
%PY% webui.py

pause
