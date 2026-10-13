@echo off
cd /d "%~dp0"
echo Miracii LAN  v0.0.1
echo Keep this window open. On the phone, open the URL printed below.
set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
"%PY%" -m app --lan --web
