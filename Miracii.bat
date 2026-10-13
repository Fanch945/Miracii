@echo off
cd /d "%~dp0"
rem One window: chat UI. Heartbeat runs in the same process. pythonw has no console.
where pythonw >nul 2>&1 && (
  start "" /D "%~dp0" pythonw -m app %*
  exit /b 0
)
where pyw >nul 2>&1 && (
  start "" /D "%~dp0" pyw -3 -m app %*
  exit /b 0
)
python -m app %*
