@echo off
setlocal
cd /d "%~dp0"
set PYTHONDONTWRITEBYTECODE=1
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" launcher.py %*
) else (
  python launcher.py %*
)
set "SCREENDESK_RESULT=%ERRORLEVEL%"
if not "%SCREENDESK_RESULT%"=="0" (
  echo Run setup-windows.cmd to prepare native Windows dependencies.
  pause
)
exit /b %SCREENDESK_RESULT%
