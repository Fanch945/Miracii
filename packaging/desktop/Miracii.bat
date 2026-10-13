@echo off
cd /d "%~dp0..\.."
where pythonw >nul 2>&1 && (
  start "" /D "%CD%" pythonw -m app %*
  exit /b 0
)
where pyw >nul 2>&1 && (
  start "" /D "%CD%" pyw -3 -m app %*
  exit /b 0
)
python -m app %*
