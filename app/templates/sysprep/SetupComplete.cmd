@echo off
REM Do NOT run setup.ps1 from SetupComplete.
REM SetupComplete runs during specialize (SYSTEM context). Running the full GuestOS
REM script there applies network/disks, then specialize cleanup can remove
REM C:\ProgramData\GuestOS (including setup.done) while leaving the applied
REM config behind — verify then hangs. Primary path: GuestOS-Setup scheduled task
REM (SYSTEM AtStartup) registered in specialize RunSynchronous.
echo [SetupComplete] %DATE% %TIME% deferring GuestOS setup.ps1 to GuestOS-Setup scheduled task >> "%WINDIR%\Temp\setup.log" 2>&1
if exist "%WINDIR%\System32\GuestOS-RegisterSetup.cmd" (
  call "%WINDIR%\System32\GuestOS-RegisterSetup.cmd" >> "%WINDIR%\Temp\setup.log" 2>&1
)
exit /b 0
