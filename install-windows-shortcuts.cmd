@echo off
setlocal
"%SystemRoot%\System32\cscript.exe" //nologo //E:JScript "%~dp0install-windows-shortcuts.js"
set "SCREENDESK_RESULT=%ERRORLEVEL%"
if /i not "%~1"=="--quiet" pause
exit /b %SCREENDESK_RESULT%
