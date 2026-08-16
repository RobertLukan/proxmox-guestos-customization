# Post-Sysprep configuration applied by GuestOS-Setup (SYSTEM AtStartup task).
# Launcher: C:\Windows\System32\GuestOS-FirstLogon.cmd (extracts SetupPs1B64).
#
# NOTE: this file is NOT autoescaped by Jinja (it is not HTML/XML). Every value
# interpolated below is validated server-side before rendering:
#   * IPv4 addresses, an integer prefix length and a DNS list (network),
#   * a MAC address (adapter selection),
#   * domain-join credentials are passed as a Base64-encoded JSON blob so no
#     credential bytes are ever interpolated into PowerShell syntax.
# Do not add unvalidated fields.

$ErrorActionPreference = 'Stop'

$pagefileLetter = $null

New-Item -ItemType Directory -Force -Path 'C:\ProgramData\GuestOS' | Out-Null
try {
    Start-Transcript -Path 'C:\ProgramData\GuestOS\setup.log' -Append | Out-Null
} catch {}

# SetupComplete.cmd must NOT invoke this script as the primary runner. After a
# successful run (or intentional reboot for pagefile/domain), skip heavy work.
function Test-GuestOsAlreadyDone {
    if (Test-Path -LiteralPath 'C:\ProgramData\GuestOS\setup.done') { return $true }
    if (Test-Path -LiteralPath 'C:\Windows\GuestOS\setup.done') { return $true }
    try {
        $st = (Get-ItemProperty -Path 'HKLM:\SOFTWARE\GuestOS' -Name SetupStatus -ErrorAction Stop).SetupStatus
        if ([string]$st -eq 'done') { return $true }
    } catch {}
    return $false
}
function Test-GuestOsPendingReboot {
    if (Test-Path -LiteralPath 'C:\ProgramData\GuestOS\setup.pending_reboot') { return $true }
    if (Test-Path -LiteralPath 'C:\Windows\GuestOS\setup.pending_reboot') { return $true }
    try {
        $st = (Get-ItemProperty -Path 'HKLM:\SOFTWARE\GuestOS' -Name SetupStatus -ErrorAction Stop).SetupStatus
        if ([string]$st -eq 'pending_reboot') { return $true }
    } catch {}
    return $false
}
if (Test-GuestOsAlreadyDone) {
    Write-Output "setup.ps1: setup already completed -- skipping."
    try { Stop-Transcript | Out-Null } catch {}
    exit 0
}

function Test-GuestOsRunningAsSystem {
    try {
        return ([Security.Principal.WindowsIdentity]::GetCurrent().User.Value -eq 'S-1-5-18')
    } catch {
        return $false
    }
}

function Invoke-GuestOsSessionEnd {
    # SYSTEM AtStartup has no interactive desktop to log off; leave the login screen.
    if (Test-GuestOsRunningAsSystem) {
        Write-Output "setup.ps1: SYSTEM runner finished; leaving login screen."
        return
    }
    Write-Output "setup.ps1: Logging off interactive session to end at lock screen."
    shutdown /l /f
}

# Overlapping invocations: first writer wins the lock.
# SetupComplete must NOT run this script as primary (specialize can delete
# ProgramData\GuestOS after SetupComplete returns). Take over when the lock
# owner PID is gone or the lock is older than 15 minutes.
$lockPath = 'C:\ProgramData\GuestOS\setup.lock'
function Test-GuestOsSetupLockStale {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $true }
    $owner = $null
    try { $owner = [int]((Get-Content -LiteralPath $Path -ErrorAction Stop | Select-Object -First 1).Trim()) } catch {}
    if ($owner -and $owner -gt 0) {
        try {
            $proc = Get-Process -Id $owner -ErrorAction Stop
            if ($proc) { return $false }
        } catch {
            return $true
        }
    }
    $ageMin = ((Get-Date) - (Get-Item -LiteralPath $Path).LastWriteTime).TotalMinutes
    return ($ageMin -ge 15)
}
function Write-GuestOsSetupMarker {
    param(
        [ValidateSet('done','failed','pending_reboot')][string]$Status,
        [string]$Detail = 'ok'
    )
    New-Item -ItemType Directory -Force -Path 'C:\ProgramData\GuestOS' | Out-Null
    New-Item -ItemType Directory -Force -Path 'C:\Windows\GuestOS' | Out-Null
    $reg = 'HKLM:\SOFTWARE\GuestOS'
    if (-not (Test-Path -LiteralPath $reg)) {
        New-Item -Path $reg -Force | Out-Null
    }
    Set-ItemProperty -Path $reg -Name SetupStatus -Value $Status -ErrorAction SilentlyContinue
    Set-ItemProperty -Path $reg -Name SetupDetail -Value $Detail -ErrorAction SilentlyContinue
    Set-ItemProperty -Path $reg -Name SetupUtc -Value ((Get-Date).ToUniversalTime().ToString('o')) -ErrorAction SilentlyContinue
    # Durable join path for verify/logs. Keep across pending_reboot → done.
    if ($Detail -eq 'already-joined') {
        Set-ItemProperty -Path $reg -Name SetupJoinMethod -Value 'odj' -ErrorAction SilentlyContinue
    } elseif ($Detail -eq 'domain-join') {
        Set-ItemProperty -Path $reg -Name SetupJoinMethod -Value 'add-computer' -ErrorAction SilentlyContinue
    }
    if ($Status -eq 'done') {
        $Detail | Set-Content -Path 'C:\ProgramData\GuestOS\setup.done' -Encoding ASCII
        $Detail | Set-Content -Path 'C:\Windows\GuestOS\setup.done' -Encoding ASCII
        Remove-Item -LiteralPath 'C:\ProgramData\GuestOS\setup.failed' -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath 'C:\Windows\GuestOS\setup.failed' -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath 'C:\ProgramData\GuestOS\setup.pending_reboot' -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath 'C:\Windows\GuestOS\setup.pending_reboot' -Force -ErrorAction SilentlyContinue
    } elseif ($Status -eq 'pending_reboot') {
        $Detail | Set-Content -Path 'C:\ProgramData\GuestOS\setup.pending_reboot' -Encoding ASCII
        $Detail | Set-Content -Path 'C:\Windows\GuestOS\setup.pending_reboot' -Encoding ASCII
        Remove-Item -LiteralPath 'C:\ProgramData\GuestOS\setup.done' -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath 'C:\Windows\GuestOS\setup.done' -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath 'C:\ProgramData\GuestOS\setup.failed' -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath 'C:\Windows\GuestOS\setup.failed' -Force -ErrorAction SilentlyContinue
    } else {
        $Detail | Set-Content -Path 'C:\ProgramData\GuestOS\setup.failed' -Encoding ASCII
        $Detail | Set-Content -Path 'C:\Windows\GuestOS\setup.failed' -Encoding ASCII
        Remove-Item -LiteralPath 'C:\ProgramData\GuestOS\setup.pending_reboot' -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath 'C:\Windows\GuestOS\setup.pending_reboot' -Force -ErrorAction SilentlyContinue
    }
}
function Clear-GuestOsWinlogonSecrets {
    # Scrub any leftover AutoLogon plaintext (should be absent with the SYSTEM runner).
    try {
        $winlogon = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
        Set-ItemProperty -Path $winlogon -Name AutoAdminLogon -Value '0' -ErrorAction SilentlyContinue
        Set-ItemProperty -Path $winlogon -Name AutoLogonCount -Value 0 -Type DWord -ErrorAction SilentlyContinue
        Remove-ItemProperty -Path $winlogon -Name DefaultPassword -ErrorAction SilentlyContinue
    } catch {}
}
function Ensure-GuestOsSetupTask {
    # Leave / re-register GuestOS-Setup so AtStartup re-runs after pending_reboot.
    try {
        $existing = Get-ScheduledTask -TaskName 'GuestOS-Setup' -ErrorAction SilentlyContinue
        if (-not $existing) {
            $reg = 'C:\Windows\System32\GuestOS-RegisterSetup.cmd'
            if (Test-Path -LiteralPath $reg) {
                & cmd.exe /c "`"$reg`""
            }
        } else {
            Enable-ScheduledTask -TaskName 'GuestOS-Setup' -ErrorAction SilentlyContinue | Out-Null
        }
        Write-Output "setup.ps1: GuestOS-Setup task left enabled for post-reboot run."
    } catch {
        Write-Output "setup.ps1: Ensure-GuestOsSetupTask warning: $($_.Exception.Message)"
    }
}
function Disable-GuestOsSetupTask {
    try {
        Unregister-ScheduledTask -TaskName 'GuestOS-Setup' -Confirm:$false -ErrorAction SilentlyContinue
        Write-Output "setup.ps1: Unregistered GuestOS-Setup scheduled task."
    } catch {
        Write-Output "setup.ps1: Disable-GuestOsSetupTask warning: $($_.Exception.Message)"
    }
}
function Scrub-GuestOsSetupArtifacts {
    # Remove durable setup.ps1 (may contain domain_join_b64). Keep SetupPs1B64
    # until final done so the AtStartup task can re-run after pending_reboot.
    try {
        Remove-ItemProperty -Path 'HKLM:\SOFTWARE\GuestOS' -Name SetupPs1B64 -ErrorAction SilentlyContinue
    } catch {}
    Remove-Item -LiteralPath 'C:\GuestOS\setup.ps1' -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath 'C:\Windows\Temp\GuestOS-setup.ps1' -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath "$env:TEMP\GuestOS-setup.ps1" -Force -ErrorAction SilentlyContinue
    if ($PSCommandPath) {
        Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
    }
}
function Wait-GuestOsNetworkReady {
    param([int]$TimeoutSeconds = 300)
    Write-Output "setup.ps1: Waiting for non-link-local IPv4 (up to ${TimeoutSeconds}s)..."
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $ips = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object {
                $_.IPAddress -and
                $_.IPAddress -notlike '127.*' -and
                $_.IPAddress -notlike '169.254.*'
            } |
            Select-Object -ExpandProperty IPAddress)
        if ($ips.Count -gt 0) {
            Write-Output "setup.ps1: network ready ($($ips[0]))."
            return
        }
        Start-Sleep -Seconds 5
    }
    Write-Output "setup.ps1: network wait timed out; continuing anyway."
}
# After an intentional pagefile/domain reboot, finalize done and exit (no re-run).
if (Test-GuestOsPendingReboot) {
    Write-Output "setup.ps1: pending_reboot observed -- marking setup done."
    Write-GuestOsSetupMarker -Status done -Detail 'ok-after-reboot'
    Scrub-GuestOsSetupArtifacts
    Disable-GuestOsSetupTask
    Clear-GuestOsWinlogonSecrets
    try { Stop-Transcript | Out-Null } catch {}
    Invoke-GuestOsSessionEnd
    exit 0
}
if (Test-Path -LiteralPath $lockPath) {
    if (-not (Test-GuestOsSetupLockStale -Path $lockPath)) {
        Write-Output "setup.ps1: setup.lock held by live process -- exiting."
        try { Stop-Transcript | Out-Null } catch {}
        exit 0
    }
    Write-Output "setup.ps1: Removing stale setup.lock."
    Remove-Item -LiteralPath $lockPath -Force -ErrorAction SilentlyContinue
}
try {
    $fs = [System.IO.File]::Open($lockPath, 'CreateNew', 'Write', 'None')
    $bytes = [System.Text.Encoding]::ASCII.GetBytes("$PID`n")
    $fs.Write($bytes, 0, $bytes.Length)
    $fs.Close()
} catch {
    Write-Output "setup.ps1: setup.lock race -- another instance won; exiting."
    try { Stop-Transcript | Out-Null } catch {}
    exit 0
}

try {

Wait-GuestOsNetworkReady

{% if manage_disks and disk_plan_b64 %}
# --- Disk reconcile (optional) --------------------------------------------
# Runs BEFORE network so a DHCP renew failure cannot skip volumes/pagefile.
# Plan arrives as Base64(JSON) so serials/letters are never shell-interpolated
# as free text beyond the validated blob.
Write-Output "setup.ps1: Starting disk reconcile."
$diskPlan = ConvertFrom-Json ([System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('{{ disk_plan_b64 }}')))

function Get-GuestOsDiskBySerial([string]$Serial) {
    Get-Disk | Where-Object {
        $_.SerialNumber -and ($_.SerialNumber.Trim() -eq $Serial)
    } | Select-Object -First 1
}

function Set-GuestOsDiskOnline {
    param($Disk, [int]$Retries = 5)
    if (-not $Disk) { return $null }
    $num = [int]$Disk.Number
    for ($i = 0; $i -lt $Retries; $i++) {
        $Disk = Get-Disk -Number $num -ErrorAction Stop
        $needOnline = [bool]$Disk.IsOffline -or ([string]$Disk.OperationalStatus -ne 'Online')
        $needRw = [bool]$Disk.IsReadOnly
        if (-not $needOnline -and -not $needRw) {
            return $Disk
        }
        # Write-Host (not Write-Output) so log lines are not mixed into the return value.
        Write-Host "setup.ps1: Bringing disk $num online (offline=$($Disk.IsOffline) status=$($Disk.OperationalStatus) readonly=$($Disk.IsReadOnly) attempt=$($i + 1))."
        try {
            if ($needOnline) {
                Set-Disk -Number $num -IsOffline $false -ErrorAction Stop
            }
            if ($needRw -or $needOnline) {
                Set-Disk -Number $num -IsReadOnly $false -ErrorAction SilentlyContinue
            }
        } catch {
            Write-Host "setup.ps1: Set-Disk online failed for disk ${num}: $($_.Exception.Message)"
        }
        Start-Sleep -Seconds 2
    }
    return (Get-Disk -Number $num -ErrorAction SilentlyContinue)
}

function Wait-GuestOsDiskBySerial([string]$Serial, [int]$TimeoutSec = 120) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    do {
        $disk = Get-GuestOsDiskBySerial $Serial
        if ($disk) {
            # New virtio disks often appear Offline until explicitly brought online.
            return (Set-GuestOsDiskOnline -Disk $disk)
        }
        Start-Sleep -Seconds 5
        # Rescan so newly attached virtio disks show SerialNumber.
        try { Update-HostStorageCache -ErrorAction SilentlyContinue } catch {}
        try { Get-Disk | Out-Null } catch {}
        # Opportunistically online any still-offline disks so serials/partitions appear.
        Get-Disk -ErrorAction SilentlyContinue | Where-Object { $_.IsOffline -or $_.IsReadOnly } | ForEach-Object {
            try { Set-GuestOsDiskOnline -Disk $_ | Out-Null } catch {}
        }
    } while ((Get-Date) -lt $deadline)
    return $null
}

function Clear-GuestOsDriveLetter([string]$Letter) {
    # Free the target letter from ANY volume (CD-ROM, leftover data, etc.).
    $L = $Letter.Substring(0, 1).ToUpper()
    Get-Volume -ErrorAction SilentlyContinue | Where-Object {
        $_.DriveLetter -and ($_.DriveLetter.ToString().ToUpper() -eq $L)
    } | ForEach-Object {
        Write-Output "setup.ps1: Freeing drive ${L}: ('$($_.FileSystemLabel)' type=$($_.DriveType))."
        cmd.exe /c "mountvol ${L}: /d" | Out-Null
    }
}

function Reset-GuestOsRecycleBin {
    param([string]$Letter)
    # After Format-Volume or drive-letter reassignment, Explorer often reports a
    # corrupted Recycle Bin ($RECYCLE.BIN). Drop it; Windows recreates a clean one.
    $Letter = $Letter.Substring(0, 1).ToUpper()
    $path = ('{0}:\{1}' -f $Letter, '$RECYCLE.BIN')
    try {
        if (Test-Path -LiteralPath $path) {
            Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction Stop
            Write-Output "setup.ps1: Reset Recycle Bin on ${Letter}:"
        }
    } catch {
        Write-Output "setup.ps1: Recycle Bin reset skipped on ${Letter}:: $($_.Exception.Message)"
    }
}

function Ensure-GuestOsVolume {
    param(
        $Disk,
        [string]$Letter,
        [string]$Label,
        [bool]$Reformat,
        [bool]$Extend
    )
    $Disk = Set-GuestOsDiskOnline -Disk $Disk
    if (-not $Disk) { throw "Disk object missing after online attempt." }
    if ($Disk.IsOffline -or [string]$Disk.OperationalStatus -ne 'Online') {
        throw "Disk $($Disk.Number) is still offline after online attempts."
    }
    $Letter = $Letter.Substring(0, 1).ToUpper()
    Clear-GuestOsDriveLetter -Letter $Letter

    $part = Get-Partition -DiskNumber $Disk.Number -ErrorAction SilentlyContinue |
        Where-Object { $_.Type -ne 'Reserved' -and $_.Type -ne 'System' -and $_.Size -gt 1MB } |
        Sort-Object Size -Descending |
        Select-Object -First 1

    if (-not $part -or $Reformat) {
        if ($part -and $Reformat) {
            Write-Output "setup.ps1: Reformatting disk $($Disk.Number) as requested."
            Clear-Disk -Number $Disk.Number -RemoveData -Confirm:$false -ErrorAction SilentlyContinue
            $Disk = Get-Disk -Number $Disk.Number
        }
        if ($Disk.PartitionStyle -eq 'Raw') {
            Initialize-Disk -Number $Disk.Number -PartitionStyle GPT -ErrorAction Stop
        }
        # Create without a letter first, then assign -- avoids races with CD-ROM letters.
        $part = New-Partition -DiskNumber $Disk.Number -UseMaximumSize -ErrorAction Stop
        Set-Partition -DiskNumber $Disk.Number -PartitionNumber $part.PartitionNumber -NewDriveLetter $Letter[0] -ErrorAction Stop
        Format-Volume -DriveLetter $Letter[0] -FileSystem NTFS -NewFileSystemLabel $Label -Confirm:$false -ErrorAction Stop | Out-Null
        Write-Output "setup.ps1: Initialized disk $($Disk.Number) as ${Letter}:"
    } else {
        if (-not $part.DriveLetter) {
            Set-Partition -DiskNumber $Disk.Number -PartitionNumber $part.PartitionNumber -NewDriveLetter $Letter[0] -ErrorAction Stop
            $part = Get-Partition -DiskNumber $Disk.Number -PartitionNumber $part.PartitionNumber
        } elseif ($part.DriveLetter.ToString().ToUpper() -ne $Letter) {
            try {
                Remove-PartitionAccessPath -DiskNumber $Disk.Number -PartitionNumber $part.PartitionNumber -AccessPath ($part.DriveLetter.ToString() + ':') -ErrorAction SilentlyContinue
            } catch {}
            Clear-GuestOsDriveLetter -Letter $Letter
            Set-Partition -DiskNumber $Disk.Number -PartitionNumber $part.PartitionNumber -NewDriveLetter $Letter[0] -ErrorAction Stop
            $part = Get-Partition -DiskNumber $Disk.Number -PartitionNumber $part.PartitionNumber
        }
        if ($Extend) {
            try {
                $supported = Get-PartitionSupportedSize -DiskNumber $Disk.Number -PartitionNumber $part.PartitionNumber -ErrorAction Stop
                if ($supported.SizeMax -gt $part.Size) {
                    Resize-Partition -DiskNumber $Disk.Number -PartitionNumber $part.PartitionNumber -Size $supported.SizeMax -ErrorAction Stop
                    Write-Output "setup.ps1: Extended partition on disk $($Disk.Number)."
                }
            } catch {
                Write-Output "setup.ps1: Extend skipped on disk $($Disk.Number): $($_.Exception.Message)"
            }
        }
    }
    Reset-GuestOsRecycleBin -Letter $Letter
    return (Get-Partition -DiskNumber $Disk.Number -ErrorAction SilentlyContinue |
        Where-Object { $_.DriveLetter } | Select-Object -First 1)
}

$pagefileLetter = $null
foreach ($d in $diskPlan) {
    $role = [string]$d.role
    $serial = [string]$d.serial
    $letter = [string]$d.drive_letter
    $label = if ($d.label) { [string]$d.label } else { $role }
    $reformat = [bool]$d.reformat
    $extend = [bool]$d.extend
    Write-Output "setup.ps1: Disk role=$role serial=$serial letter=$letter"
    $disk = Wait-GuestOsDiskBySerial $serial -TimeoutSec 120
    if (-not $disk) {
        throw "Disk serial $serial not found after wait."
    }
    $disk = Set-GuestOsDiskOnline -Disk $disk
    if (-not $disk -or $disk.IsOffline -or [string]$disk.OperationalStatus -ne 'Online') {
        throw "Disk serial $serial could not be brought online."
    }
    if ($role -eq 'os') {
        # Extend C: if the PVE boot disk was grown.
        try {
            $cPart = Get-Partition -DriveLetter C -ErrorAction Stop
            if ($extend) {
                $supported = Get-PartitionSupportedSize -DiskNumber $cPart.DiskNumber -PartitionNumber $cPart.PartitionNumber
                if ($supported.SizeMax -gt $cPart.Size) {
                    Resize-Partition -DiskNumber $cPart.DiskNumber -PartitionNumber $cPart.PartitionNumber -Size $supported.SizeMax -ErrorAction Stop
                    Write-Output "setup.ps1: Extended OS volume C:."
                }
            }
        } catch {
            Write-Output "setup.ps1: OS extend skipped: $($_.Exception.Message)"
        }
        continue
    }
    try {
        $null = Ensure-GuestOsVolume -Disk $disk -Letter $letter -Label $label -Reformat $reformat -Extend $extend
        if ($role -eq 'pagefile' -and [bool]$d.ensure_pagefile) {
            $pagefileLetter = $letter.ToUpper()
        }
    } catch {
        throw "Disk role=$role failed: $($_.Exception.Message)"
    }
}

if ($pagefileLetter) {
    # Prefer registry over Win32_PageFileSetting/CIM: on Server 2019 under SetupComplete,
    # Set-CimInstance AutomaticManagedPagefile has been observed to abort the whole script
    # with no catch (process dies right after "Configuring pagefile").
    Write-Output "setup.ps1: Configuring pagefile on ${pagefileLetter}: via registry."
    try {
        $path = "${pagefileLetter}:\pagefile.sys"
        $mm = 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management'
        # Size from plan when available (min_size_gb), else 2048-8192.
        $maxMb = 8192
        foreach ($d in @($diskPlan)) {
            if ([string]$d.role -eq 'pagefile' -and $d.min_size_gb) {
                $maxMb = [Math]::Max(2048, ([int]$d.min_size_gb) * 1024)
            }
        }
        $minMb = [Math]::Min(2048, $maxMb)
        # Disable automatic pagefile management, then pin a single path.
        Set-ItemProperty -Path $mm -Name 'PagingFiles' -Value @("$path $minMb $maxMb") -Type MultiString -ErrorAction Stop
        try {
            Set-ItemProperty -Path $mm -Name 'AutomaticManagedPagefile' -Value 0 -ErrorAction SilentlyContinue
        } catch {}
        # Remove leftover pagefile.sys from other fixed volumes (old template paging disk).
        Get-Volume -ErrorAction SilentlyContinue | Where-Object {
            $_.DriveLetter -and ($_.DriveType -eq 'Fixed') -and
            ($_.DriveLetter.ToString().ToUpper() -ne $pagefileLetter.ToUpper())
        } | ForEach-Object {
            $other = ($_.DriveLetter.ToString().ToUpper() + ':\pagefile.sys')
            if (Test-Path -LiteralPath $other) {
                Write-Output "setup.ps1: Removing stale pagefile at $other"
                try {
                    attrib -s -h $other 2>$null
                    Remove-Item -LiteralPath $other -Force -ErrorAction Stop
                } catch {
                    Write-Output "setup.ps1: Could not remove $other : $($_.Exception.Message)"
                }
            }
        }
        Write-Output "setup.ps1: Pagefile set to $path $minMb-$maxMb (takes effect after reboot)."
    } catch {
        throw "Pagefile configuration failed: $($_.Exception.Message)"
    }
}
Write-Output "setup.ps1: Disk reconcile complete."
{% endif %}

# --- Network (one or more NICs from validated Base64 JSON) -----------------
# Each entry: mac, dhcp, ip, prefix, gateway, dns[], ipv6, ip6, prefix6, gw6
$nicsBlob = '{{ nics_b64 }}'
$nics = @(ConvertFrom-Json ([System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($nicsBlob))))
if (-not $nics -or $nics.Count -lt 1) {
    throw "No NIC configuration was provided to setup.ps1."
}

function Get-GuestOsAdapterByMac {
    param([string]$MacColon)
    $norm = ($MacColon -replace ':', '-').ToUpper()
    $adapter = $null
    if ($norm) {
        $adapter = Get-NetAdapter -Physical | Where-Object { $_.MacAddress.ToUpper() -eq $norm } | Select-Object -First 1
    }
    if (-not $adapter) {
        $adapter = Get-NetAdapter -Physical | Sort-Object ifIndex | Select-Object -First 1
    }
    return $adapter
}

function Set-GuestOsNicConfig {
    param($Nic, $Adapter)
    if (-not $Adapter) { throw "No physical network adapter found for NIC config." }
    $ifIndex = $Adapter.ifIndex
    Write-Output "setup.ps1: Using adapter '$($Adapter.Name)' (ifIndex $ifIndex, MAC $($Adapter.MacAddress))."

    if ($Nic.dhcp) {
        Get-NetIPAddress -InterfaceIndex $ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object { $_.IPAddress -notlike '127.*' -and $_.PrefixOrigin -eq 'Manual' } |
            ForEach-Object {
                Remove-NetIPAddress -InterfaceIndex $ifIndex -IPAddress $_.IPAddress -Confirm:$false -ErrorAction SilentlyContinue
            }
        Get-NetRoute -InterfaceIndex $ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object { $_.DestinationPrefix -eq '0.0.0.0/0' -and $_.NextHop -ne '0.0.0.0' } |
            ForEach-Object {
                if ($_.Protocol -eq 'NetMgmt' -or $_.Protocol -eq 'Local') {
                    Remove-NetRoute -InterfaceIndex $ifIndex -DestinationPrefix $_.DestinationPrefix -Confirm:$false -ErrorAction SilentlyContinue
                }
            }
        Write-Output "setup.ps1: Enabling DHCP on ifIndex $ifIndex."
        Set-NetIPInterface -InterfaceIndex $ifIndex -Dhcp Enabled
        $dns = @($Nic.dns)
        if ($dns.Count -gt 0) {
            Set-DnsClientServerAddress -InterfaceIndex $ifIndex -ServerAddresses $dns
        } else {
            Set-DnsClientServerAddress -InterfaceIndex $ifIndex -ResetServerAddresses
        }
        Write-Output "setup.ps1: Renewing DHCP lease on '$($Adapter.Name)' (60s timeout)."
        $renewLog = 'C:\ProgramData\GuestOS\dhcprenew.txt'
        $renewArgs = "/c ipconfig /renew `"$($Adapter.Name)`" >`"$renewLog`" 2>&1"
        $renewProc = Start-Process -FilePath 'cmd.exe' -ArgumentList $renewArgs -PassThru -WindowStyle Hidden
        if (-not $renewProc.WaitForExit(60000)) {
            Write-Output "setup.ps1: WARNING: ipconfig /renew timed out after 60s; killing."
            try { Stop-Process -Id $renewProc.Id -Force -ErrorAction SilentlyContinue } catch {}
        }
        if (Test-Path -LiteralPath $renewLog) {
            Get-Content -LiteralPath $renewLog -ErrorAction SilentlyContinue | ForEach-Object { Write-Output $_ }
        }
    } else {
        Get-NetIPAddress -InterfaceIndex $ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object { $_.IPAddress -notlike '127.*' } |
            ForEach-Object {
                Remove-NetIPAddress -InterfaceIndex $ifIndex -IPAddress $_.IPAddress -Confirm:$false -ErrorAction SilentlyContinue
            }
        Get-NetRoute -InterfaceIndex $ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object { $_.DestinationPrefix -eq '0.0.0.0/0' -or $_.NextHop -ne '0.0.0.0' } |
            ForEach-Object {
                Remove-NetRoute -InterfaceIndex $ifIndex -DestinationPrefix $_.DestinationPrefix -Confirm:$false -ErrorAction SilentlyContinue
            }
        $ip = [string]$Nic.ip
        $prefix = [int]$Nic.prefix
        $gateway = [string]$Nic.gateway
        $dns = @($Nic.dns)
        Write-Output "setup.ps1: Static IP=$ip/$prefix GW=$(if ($gateway) { $gateway } else { '(none)' }) DNS=$($dns -join ',')"
        Set-NetIPInterface -InterfaceIndex $ifIndex -Dhcp Disabled -ErrorAction SilentlyContinue
        $configured = $false
        for ($i = 0; $i -lt 5 -and -not $configured; $i++) {
            try {
                $existing = Get-NetIPAddress -InterfaceIndex $ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
                    Where-Object { $_.IPAddress -eq $ip }
                if (-not $existing) {
                    New-NetIPAddress -InterfaceIndex $ifIndex -IPAddress $ip -PrefixLength $prefix -ErrorAction Stop | Out-Null
                }
                # Only set a default route when a gateway was provided. Extra NICs
                # commonly omit GW so Windows does not get competing defaults.
                if ($gateway) {
                    $route = Get-NetRoute -InterfaceIndex $ifIndex -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
                        Where-Object { $_.NextHop -eq $gateway } | Select-Object -First 1
                    if (-not $route) {
                        New-NetRoute -InterfaceIndex $ifIndex -DestinationPrefix '0.0.0.0/0' -NextHop $gateway -ErrorAction Stop | Out-Null
                    }
                }
                $configured = $true
                Write-Output "setup.ps1: Static addressing applied."
            } catch {
                Write-Output "setup.ps1: Static IP attempt $($i + 1) failed: $($_.Exception.Message)"
                Start-Sleep -Seconds 5
            }
        }
        if (-not $configured) {
            if ($gateway) {
                throw "Failed to apply static IP $ip/$prefix via gateway $gateway."
            }
            throw "Failed to apply static IP $ip/$prefix."
        }
        if ($dns.Count -gt 0) {
            Set-DnsClientServerAddress -InterfaceIndex $ifIndex -ServerAddresses $dns
        } else {
            Set-DnsClientServerAddress -InterfaceIndex $ifIndex -ResetServerAddresses
        }
    }

    if ($Nic.ipv6) {
        $ip6 = [string]$Nic.ip6
        $prefix6 = [int]$Nic.prefix6
        $gw6 = [string]$Nic.gw6
        Write-Output "setup.ps1: Configuring IPv6 $ip6/$prefix6 GW=$gw6"
        Get-NetIPAddress -InterfaceIndex $ifIndex -AddressFamily IPv6 -ErrorAction SilentlyContinue |
            Where-Object { $_.PrefixOrigin -eq 'Manual' } |
            ForEach-Object {
                Remove-NetIPAddress -InterfaceIndex $ifIndex -IPAddress $_.IPAddress -Confirm:$false -ErrorAction SilentlyContinue
            }
        $existing6 = Get-NetIPAddress -InterfaceIndex $ifIndex -AddressFamily IPv6 -ErrorAction SilentlyContinue |
            Where-Object { $_.IPAddress -eq $ip6 }
        if (-not $existing6) {
            New-NetIPAddress -InterfaceIndex $ifIndex -IPAddress $ip6 -PrefixLength $prefix6 -AddressFamily IPv6 -ErrorAction Stop | Out-Null
        }
        if ($gw6) {
            $route6 = Get-NetRoute -InterfaceIndex $ifIndex -DestinationPrefix '::/0' -ErrorAction SilentlyContinue |
                Where-Object { $_.NextHop -eq $gw6 } | Select-Object -First 1
            if (-not $route6) {
                New-NetRoute -InterfaceIndex $ifIndex -DestinationPrefix '::/0' -NextHop $gw6 -AddressFamily IPv6 -ErrorAction SilentlyContinue | Out-Null
            }
        }
    }
}

$nicIndex = 0
foreach ($nic in $nics) {
    $nicIndex++
    Write-Output "setup.ps1: Configuring NIC $nicIndex of $($nics.Count)."
    $adapter = Get-GuestOsAdapterByMac -MacColon ([string]$nic.mac)
    if (-not $adapter -and $nicIndex -gt 1) {
        # For extra NICs without MAC match, pick unused physical adapters by ifIndex order.
        $used = @()
        # best-effort: take Nth physical adapter
        $adapter = Get-NetAdapter -Physical | Sort-Object ifIndex | Select-Object -Skip ($nicIndex - 1) -First 1
    }
    Set-GuestOsNicConfig -Nic $nic -Adapter $adapter
}
Write-Output "setup.ps1: Network configuration complete."

# --- Local accounts --------------------------------------------------------
# Sysprep /generalize does NOT remove accounts that existed on the template
# (e.g. an interactive "rl" user). Unattend also creates GuestOSOobe so client
# OOBE can finish without AutoLogon. Enable the built-in Administrator and drop
# every other local account so clones boot to a clean admin-only local state.
# Built-in / system accounts are left alone.
Write-Output "setup.ps1: Enabling built-in Administrator account."
try {
    Enable-LocalUser -Name 'Administrator' -ErrorAction Stop
} catch {
    # Fallback for editions where Enable-LocalUser is picky.
    net user Administrator /active:yes | Out-Null
}

$keepLocalUsers = @(
    'Administrator',
    'Guest',
    'DefaultAccount',
    'WDAGUtilityAccount',
    'defaultuser0'
)
Get-LocalUser | Where-Object { $keepLocalUsers -notcontains $_.Name } | ForEach-Object {
    Write-Output "setup.ps1: Removing leftover local user '$($_.Name)'."
    try {
        Remove-LocalUser -Name $_.Name -ErrorAction Stop
    } catch {
        Write-Output "setup.ps1: Could not remove '$($_.Name)': $($_.Exception.Message)"
    }
}

# Scrub plaintext admin password from answer files left on disk (Winlogon
# DefaultPassword should already be absent without AutoLogon).
Remove-Item -LiteralPath 'C:\Windows\System32\Sysprep\unattended.xml' -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath 'C:\Windows\Panther\unattend.xml' -Force -ErrorAction SilentlyContinue

{% if join_domain %}
# --- Domain join -----------------------------------------------------------
# Credentials arrive as Base64(JSON{domain,username,password[,ou]}) so nothing
# sensitive is interpolated into PowerShell syntax and the password is never
# written to the log.
$blob = '{{ domain_join_b64 }}'
$j = ConvertFrom-Json ([System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($blob)))
$sec = ConvertTo-SecureString $j.password -AsPlainText -Force
$cred = New-Object System.Management.Automation.PSCredential($j.username, $sec)

function Format-DomainJoinError([string]$msg) {
  $m = $msg.ToLowerInvariant()
  if ($m -match 'logon failure|username or password is incorrect|0x8007052e|52e') {
    return "invalid_credentials: $msg"
  }
  if ($m -match 'already a member|already exists|0x80071392|directory object') {
    return "computer_account: $msg"
  }
  if ($m -match 'network path|rpc server|domain controller|dns|timeout|unreachable|0x8007054b') {
    return "unreachable: $msg"
  }
  if ($m -match 'access is denied|insufficient|0x80070005|permission') {
    return "permission_denied: $msg"
  }
  if ($m -match 'account|locked|disabled|password expired|logon hours') {
    return "account_restricted: $msg"
  }
  return "other: $msg"
}

Write-Output "setup.ps1: Domain join starting domain=$($j.domain) username=$($j.username)$(if ($j.ou) { " ou=$($j.ou)" } else { '' })."

# Wait for the network/DNS to be usable, then join with a few retries.
# UnattendedJoin (DHCP specialize) may already have joined; skip Add-Computer.
$alreadyJoined = $false
try {
    $cs = Get-CimInstance Win32_ComputerSystem
    $alreadyJoined = [bool]$cs.PartOfDomain
    if ($alreadyJoined) {
        Write-Output "setup.ps1: Already domain-joined ($($cs.Domain)); skipping Add-Computer."
        Write-Output "setup.ps1: join-path method=odj"
        try {
            w32tm /resync /force 2>&1 | ForEach-Object { Write-Output "setup.ps1: w32tm: $_" }
        } catch {
            Write-Output "setup.ps1: w32tm /resync skipped: $($_.Exception.Message)"
        }
    }
} catch {
    Write-Output "setup.ps1: Could not read PartOfDomain: $($_.Exception.Message)"
}
$joined = $alreadyJoined
$lastJoinErr = ''
for ($i = 0; $i -lt 10 -and -not $joined; $i++) {
    try {
        {% if domain_ou %}
        Add-Computer -DomainName $j.domain -OUPath $j.ou -Credential $cred -Force -ErrorAction Stop
        {% else %}
        Add-Computer -DomainName $j.domain -Credential $cred -Force -ErrorAction Stop
        {% endif %}
        $joined = $true
        Write-Output "setup.ps1: Joined domain $($j.domain) as $($j.username)."
        Write-Output "setup.ps1: join-path method=add-computer"
        # Best-effort: pull time from the domain (PDC) before the membership reboot.
        # Does not fail the job if the DC clock/NTP is wrong — that is an AD/lab issue.
        try {
            w32tm /resync /force 2>&1 | ForEach-Object { Write-Output "setup.ps1: w32tm: $_" }
        } catch {
            Write-Output "setup.ps1: w32tm /resync skipped: $($_.Exception.Message)"
        }
    } catch {
        $lastJoinErr = Format-DomainJoinError $_.Exception.Message
        Write-Output "setup.ps1: Domain join attempt $($i + 1) failed ($lastJoinErr)"
        Start-Sleep -Seconds 15
    }
}

# Do not scrub SetupPs1B64 before an intentional reboot — the AtStartup task must
# re-extract setup.ps1 to mark done. Scrub only on final done paths.
# Always reboot once after a successful join, including Offline Domain Join
# (already a member before this script). That second boot is the first
# interactive logon, so Explorer is not left queued on the OOBE session.
if ($joined) {
    if ($alreadyJoined) {
        Write-Output "setup.ps1: Restarting so first interactive logon is a clean session."
        $rebootDetail = 'already-joined'
    } else {
        Write-Output "setup.ps1: Restarting to finalize domain membership."
        $rebootDetail = 'domain-join'
    }
    Ensure-GuestOsSetupTask
    Write-GuestOsSetupMarker -Status pending_reboot -Detail $rebootDetail
    try { Stop-Transcript | Out-Null } catch {}
    shutdown /r /t 5
} elseif ($pagefileLetter) {
    Write-Output "setup.ps1: Restarting so pagefile on ${pagefileLetter}: becomes active."
    Ensure-GuestOsSetupTask
    Write-GuestOsSetupMarker -Status pending_reboot -Detail 'pagefile'
    try { Stop-Transcript | Out-Null } catch {}
    shutdown /r /t 5
} else {
    Write-Output "setup.ps1: Domain join FAILED after retries domain=$($j.domain) username=$($j.username) last=$lastJoinErr; leaving machine in workgroup."
    Write-GuestOsSetupMarker -Status done -Detail 'domain-join-failed'
    Scrub-GuestOsSetupArtifacts
    Disable-GuestOsSetupTask
    Clear-GuestOsWinlogonSecrets
    try { Stop-Transcript | Out-Null } catch {}
    Invoke-GuestOsSessionEnd
}
{% else %}
# Workgroup (non-domain) path.
$workgroup = '{{ workgroup | default("WORKGROUP", true) }}'
if ($workgroup) {
    try {
        $current = (Get-CimInstance Win32_ComputerSystem).Workgroup
        if ($current -ne $workgroup) {
            Write-Output "setup.ps1: Setting workgroup to $workgroup (was $current)."
            Add-Computer -WorkGroupName $workgroup -Force -ErrorAction Stop
        } else {
            Write-Output "setup.ps1: Already in workgroup $workgroup."
        }
    } catch {
        Write-Output "setup.ps1: Workgroup set warning: $($_.Exception.Message)"
    }
}

if ($pagefileLetter) {
    Write-Output "setup.ps1: Restarting so pagefile on ${pagefileLetter}: becomes active."
    Ensure-GuestOsSetupTask
    Write-GuestOsSetupMarker -Status pending_reboot -Detail 'pagefile'
    try { Stop-Transcript | Out-Null } catch {}
    shutdown /r /t 5
} else {
    Write-GuestOsSetupMarker -Status done -Detail 'ok'
    Scrub-GuestOsSetupArtifacts
    Disable-GuestOsSetupTask
    Clear-GuestOsWinlogonSecrets
    try { Stop-Transcript | Out-Null } catch {}
    Invoke-GuestOsSessionEnd
}
{% endif %}
} catch {
    $msg = $_.Exception.Message
    Write-Output "setup.ps1: FATAL: $msg"
    try { Write-GuestOsSetupMarker -Status failed -Detail ("fail: " + $msg) } catch {
        New-Item -ItemType Directory -Force -Path 'C:\ProgramData\GuestOS' | Out-Null
        ("fail: " + $msg) | Set-Content -Path 'C:\ProgramData\GuestOS\setup.failed' -Encoding ASCII
    }
    try { Stop-Transcript | Out-Null } catch {}
    exit 1
} finally {
    Remove-Item -LiteralPath 'C:\ProgramData\GuestOS\setup.lock' -Force -ErrorAction SilentlyContinue
}
