@echo off
setlocal
cd /d "%~dp0"
title ScreenDesk - Native Windows Setup
python -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)"
if errorlevel 1 (
  echo Install Python 3.9+ for Windows and add python to PATH, then run this file again.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" python -m venv .venv
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install -r requirements-windows.txt
if errorlevel 1 goto failed
call install-windows-shortcuts.cmd --quiet
if errorlevel 1 goto failed
start "" "data\desktop\ScreenDesk.exe" %*
exit /b 0
:failed
echo Setup failed. Send the error above to Codex.
pause
exit /b 1
