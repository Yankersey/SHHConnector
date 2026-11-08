@echo off
setlocal
if exist "%~dp0.venv\Scripts\python.exe" (
  "%~dp0.venv\Scripts\python.exe" "%~dp0install_windows_app.py"
) else (
  python "%~dp0install_windows_app.py"
)
if errorlevel 1 exit /b 1
"%SystemRoot%\System32\cscript.exe" //nologo //E:JScript "%~dp0install-windows-shortcuts.js"
set "SCREENDESK_RESULT=%ERRORLEVEL%"
if /i not "%~1"=="--quiet" pause
exit /b %SCREENDESK_RESULT%
