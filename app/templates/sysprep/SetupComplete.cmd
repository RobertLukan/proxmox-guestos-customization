@echo off
REM Windows automatically runs %WINDIR%\Setup\Scripts\SetupComplete.cmd once,
REM as SYSTEM, at the end of setup (after the Sysprep specialize/oobe passes).
REM We use it to invoke the network configuration script written alongside it.
echo [SetupComplete] %DATE% %TIME% running setup.ps1 >> "%WINDIR%\Temp\setup.log" 2>&1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" >> "%WINDIR%\Temp\setup.log" 2>&1
echo [SetupComplete] %DATE% %TIME% setup.ps1 exit code %ERRORLEVEL% >> "%WINDIR%\Temp\setup.log" 2>&1
