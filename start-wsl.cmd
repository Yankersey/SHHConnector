@echo off
setlocal
cd /d "%~dp0"
wsl.exe --distribution Ubuntu --cd "%CD%" --exec sh ./start.sh %*
set "SCREENDESK_RESULT=%ERRORLEVEL%"
if not "%SCREENDESK_RESULT%"=="0" pause
exit /b %SCREENDESK_RESULT%
