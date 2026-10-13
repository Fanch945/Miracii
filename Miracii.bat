@echo off
cd /d "%~dp0"
rem One window: chat UI. Heartbeat runs in the same process. pythonw has no console.
rem The project venv is authoritative (requirements.txt); PATH pythonw is only a fallback.
if exist "%~dp0.venv\Scripts\pythonw.exe" (
  start "" /D "%~dp0" "%~dp0.venv\Scripts\pythonw.exe" -m app %*
  exit /b 0
)
where pythonw >nul 2>&1 && (
  start "" /D "%~dp0" pythonw -m app %*
  exit /b 0
)
where pyw >nul 2>&1 && (
  start "" /D "%~dp0" pyw -3 -m app %*
  exit /b 0
)
python -m app %*
