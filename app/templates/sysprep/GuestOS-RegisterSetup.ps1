# Register GuestOS-Setup scheduled task (SYSTEM).
# Invoked from specialize RunSynchronous via GuestOS-RegisterSetup.cmd.
#
# AtStartup alone is not enough on the first post-Sysprep boot: specialize
# registers the task after the boot's startup event has already fired, so the
# AtStartup trigger is missed until a later reboot (e.g. domain-join). A Once
# trigger with short repetition covers that first-boot window; AtStartup covers
# pending_reboot. setup.ps1 unregisters the task when done.
$ErrorActionPreference = 'Stop'
$taskName = 'GuestOS-Setup'
$launcher = 'C:\Windows\System32\GuestOS-FirstLogon.cmd'

if (-not (Test-Path -LiteralPath $launcher)) {
    Write-Output "GuestOS-RegisterSetup: missing $launcher (will still register task)."
}

$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument ('/c "' + $launcher + '"')

$startup = New-ScheduledTaskTrigger -AtStartup
# Delay so OOBE/network stack can settle before setup.ps1 runs.
try { $startup.Delay = 'PT45S' } catch {}

# First-boot catch-up: fire soon, then retry every 2 minutes. Duration must
# survive an ODJ/NTP clock jump (template CMOS can be hours behind the DC).
# A 2-hour window registered at the stale clock expires immediately after sync.
$onceAt = (Get-Date).AddMinutes(2)
$once = New-ScheduledTaskTrigger `
    -Once `
    -At $onceAt `
    -RepetitionInterval (New-TimeSpan -Minutes 2) `
    -RepetitionDuration (New-TimeSpan -Hours 24)

$principal = New-ScheduledTaskPrincipal `
    -UserId 'SYSTEM' `
    -LogonType ServiceAccount `
    -RunLevel Highest

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger @($startup, $once) `
    -Principal $principal `
    -Settings $settings `
    -Force | Out-Null

Write-Output "GuestOS-RegisterSetup: registered task $taskName (SYSTEM AtStartup +45s; Once+2m repeat/24h for first boot)."
