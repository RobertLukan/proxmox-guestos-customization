@echo off
REM Register GuestOS-Setup: SYSTEM AtStartup task that runs GuestOS-FirstLogon.cmd.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0GuestOS-RegisterSetup.ps1"
exit /b %ERRORLEVEL%
