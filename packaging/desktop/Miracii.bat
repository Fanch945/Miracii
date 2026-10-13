@echo off
cd /d "%~dp0..\.."
rem The project venv is authoritative; PATH pythonw is only a fallback.
if exist "%CD%\.venv\Scripts\pythonw.exe" (
  start "" /D "%CD%" "%CD%\.venv\Scripts\pythonw.exe" -m app %*
  exit /b 0
)
where pythonw >nul 2>&1 && (
  start "" /D "%CD%" pythonw -m app %*
  exit /b 0
)
where pyw >nul 2>&1 && (
  start "" /D "%CD%" pyw -3 -m app %*
  exit /b 0
)
python -m app %*
