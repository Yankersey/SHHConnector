@echo off
setlocal
where wsl.exe >nul 2>nul
if errorlevel 1 (
  echo WSL is required. Open Administrator PowerShell and run: wsl --install
  echo Then restart Windows and install Python 3 and OpenSSH in Ubuntu.
  pause
  exit /b 1
)
rem WSL inherits the Windows working directory; wslpath converts it safely.
for /f "usebackq delims=" %%P in (`wsl.exe --exec wslpath -u "%~dp0."`) do set "SCREENDESK_WSL_PATH=%%P"
if not defined SCREENDESK_WSL_PATH (
  echo Cannot resolve the project path. Start Ubuntu and run: sh start.sh
  pause
  exit /b 1
)
wsl.exe --cd "%SCREENDESK_WSL_PATH%" --exec sh ./start.sh %*
set "SCREENDESK_RESULT=%ERRORLEVEL%"
if not "%SCREENDESK_RESULT%"=="0" pause
exit /b %SCREENDESK_RESULT%
