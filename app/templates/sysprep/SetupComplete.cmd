@echo off
REM Optional path: Windows may run this after Setup if the Scripts folder survived.
REM Primary path is unattend FirstLogonCommands → C:\ProgramData\GuestOS\setup.ps1
REM (Sysprep /generalize often removes C:\Windows\Setup\Scripts on Server 2019).
echo [SetupComplete] %DATE% %TIME% running ProgramData setup.ps1 >> "%WINDIR%\Temp\setup.log" 2>&1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\ProgramData\GuestOS\setup.ps1" >> "%WINDIR%\Temp\setup.log" 2>&1
echo [SetupComplete] %DATE% %TIME% setup.ps1 exit code %ERRORLEVEL% >> "%WINDIR%\Temp\setup.log" 2>&1
