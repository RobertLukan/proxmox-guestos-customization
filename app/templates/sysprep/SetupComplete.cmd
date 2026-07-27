@echo off
REM Do NOT run setup.ps1 from SetupComplete.
REM SetupComplete runs during specialize (SYSTEM context). Running the full GuestOS
REM script there applies network/disks, then specialize cleanup can remove
REM C:\ProgramData\GuestOS (including setup.done) while leaving the applied
REM config behind — verify then hangs and FirstLogon re-fires a missing script.
REM Primary path: unattend FirstLogonCommands after AutoLogon.
echo [SetupComplete] %DATE% %TIME% deferring GuestOS setup.ps1 to FirstLogonCommands >> "%WINDIR%\Temp\setup.log" 2>&1
exit /b 0
