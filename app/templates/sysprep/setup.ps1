# Post-Sysprep configuration applied by SetupComplete.cmd.
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

# SetupComplete.cmd AND FirstLogonCommands both invoke this script. After a
# successful run (or intentional reboot for pagefile), skip heavy work so a
# second launch cannot hang on pagefile/DHCP again.
if (Test-Path -LiteralPath 'C:\ProgramData\GuestOS\setup.done') {
    Write-Output "setup.ps1: setup.done present -- skipping (already completed)."
    try { Stop-Transcript | Out-Null } catch {}
    exit 0
}

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

function Clear-GuestOsCdRomLetter([string]$Letter) {
    # Templates often mount virtio/ISO images on D:/E:, which blocks New-Partition -DriveLetter.
    $L = $Letter.Substring(0, 1).ToUpper()
    Get-Volume -ErrorAction SilentlyContinue | Where-Object {
        $_.DriveLetter -and ($_.DriveLetter.ToString().ToUpper() -eq $L) -and ([string]$_.DriveType -eq 'CD-ROM')
    } | ForEach-Object {
        Write-Output "setup.ps1: Freeing CD-ROM drive ${L}: ('$($_.FileSystemLabel)')."
        cmd.exe /c "mountvol ${L}: /d" | Out-Null
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
    Clear-GuestOsCdRomLetter -Letter $Letter

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
            Clear-GuestOsCdRomLetter -Letter $Letter
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
        Write-Output "setup.ps1: WARNING: disk serial $serial not found after wait."
        continue
    }
    $disk = Set-GuestOsDiskOnline -Disk $disk
    if (-not $disk -or $disk.IsOffline -or [string]$disk.OperationalStatus -ne 'Online') {
        Write-Output "setup.ps1: WARNING: disk serial $serial could not be brought online."
        continue
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
        Write-Output "setup.ps1: Disk role=$role failed: $($_.Exception.Message)"
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
        # Explicit PagingFiles entry disables system-managed pagefile (?:\pagefile.sys).
        Set-ItemProperty -Path $mm -Name 'PagingFiles' -Value @("$path 2048 8192") -Type MultiString -ErrorAction Stop
        Write-Output "setup.ps1: Pagefile set to $path 2048-8192 (takes effect after reboot)."
    } catch {
        Write-Output "setup.ps1: Pagefile configuration failed: $($_.Exception.Message)"
    }
}
Write-Output "setup.ps1: Disk reconcile complete."
{% endif %}

$mac = '{{ primary_mac_address | default("", true) }}'.Replace(':', '-').ToUpper()

# Select the target adapter by MAC; fall back to the first physical adapter so a
# single-NIC VM still configures correctly even if the MAC could not be resolved.
$adapter = $null
if ($mac) {
    $adapter = Get-NetAdapter -Physical | Where-Object { $_.MacAddress.ToUpper() -eq $mac } | Select-Object -First 1
}
if (-not $adapter) {
    Write-Output "setup.ps1: MAC '$mac' not matched; falling back to first physical adapter."
    $adapter = Get-NetAdapter -Physical | Sort-Object ifIndex | Select-Object -First 1
}
if (-not $adapter) { throw "No physical network adapter found." }

$ifIndex = $adapter.ifIndex
Write-Output "setup.ps1: Using adapter '$($adapter.Name)' (ifIndex $ifIndex, MAC $($adapter.MacAddress))."

# Clear any existing IPv4 address / default route first so re-runs are idempotent.
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

{% if use_dhcp %}
Write-Output "setup.ps1: Enabling DHCP on ifIndex $ifIndex."
Set-NetIPInterface -InterfaceIndex $ifIndex -Dhcp Enabled
{% if dns_list %}
# Explicit DNS override (e.g. to reach the domain controller) even under DHCP.
Set-DnsClientServerAddress -InterfaceIndex $ifIndex -ServerAddresses @({% for d in dns_list %}'{{ d }}'{% if not loop.last %}, {% endif %}{% endfor %})
{% else %}
Set-DnsClientServerAddress -InterfaceIndex $ifIndex -ResetServerAddresses
{% endif %}
# Clearing addresses above drops the lease; renew so the guest is not left without IPv4.
# Redirect inside cmd.exe -- never pipe ipconfig stderr into PowerShell (2>&1 + Stop
# aborts the whole script on DHCP timeout).
Write-Output "setup.ps1: Renewing DHCP lease on '$($adapter.Name)'."
$renewLog = 'C:\ProgramData\GuestOS\dhcprenew.txt'
cmd.exe /c "ipconfig /renew `"$($adapter.Name)`" >`"C:\ProgramData\GuestOS\dhcprenew.txt`" 2>&1" | Out-Null
if (Test-Path -LiteralPath $renewLog) {
    Get-Content -LiteralPath $renewLog -ErrorAction SilentlyContinue | ForEach-Object { Write-Output $_ }
}
for ($i = 0; $i -lt 12; $i++) {
    $lease = Get-NetIPAddress -InterfaceIndex $ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notlike '169.254.*' -and $_.IPAddress -notlike '127.*' } |
        Select-Object -First 1
    if ($lease) {
        Write-Output "setup.ps1: DHCP lease: $($lease.IPAddress)"
        break
    }
    Start-Sleep -Seconds 5
}
if (-not $lease) {
    Write-Output "setup.ps1: WARNING: no DHCP lease yet; Windows may still be acquiring one."
}
{% else %}
$ip      = '{{ ip_address }}'
$prefix  = {{ netmask_cidr }}
$gateway = '{{ gateway }}'
$dns     = @({% for d in dns_list %}'{{ d }}'{% if not loop.last %}, {% endif %}{% endfor %})
Write-Output "setup.ps1: Static IP=$ip/$prefix GW=$gateway DNS=$($dns -join ',')"
Set-NetIPInterface -InterfaceIndex $ifIndex -Dhcp Disabled -ErrorAction SilentlyContinue
# Server 2016/2019: combining -DefaultGateway with New-NetIPAddress often fails when a
# residual default route exists. Set address and route as separate steps with retries.
$configured = $false
for ($i = 0; $i -lt 5 -and -not $configured; $i++) {
    try {
        $existing = Get-NetIPAddress -InterfaceIndex $ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object { $_.IPAddress -eq $ip }
        if (-not $existing) {
            New-NetIPAddress -InterfaceIndex $ifIndex -IPAddress $ip -PrefixLength $prefix -ErrorAction Stop | Out-Null
        }
        $route = Get-NetRoute -InterfaceIndex $ifIndex -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
            Where-Object { $_.NextHop -eq $gateway } | Select-Object -First 1
        if (-not $route) {
            New-NetRoute -InterfaceIndex $ifIndex -DestinationPrefix '0.0.0.0/0' -NextHop $gateway -ErrorAction Stop | Out-Null
        }
        $configured = $true
        Write-Output "setup.ps1: Static addressing applied."
    } catch {
        Write-Output "setup.ps1: Static IP attempt $($i + 1) failed: $($_.Exception.Message)"
        Start-Sleep -Seconds 5
    }
}
if (-not $configured) {
    throw "Failed to apply static IP $ip/$prefix via gateway $gateway."
}
if ($dns.Count -gt 0) {
    Set-DnsClientServerAddress -InterfaceIndex $ifIndex -ServerAddresses $dns
} else {
    Set-DnsClientServerAddress -InterfaceIndex $ifIndex -ResetServerAddresses
}
{% endif %}

Write-Output "setup.ps1: Network configuration complete."

# --- Local accounts --------------------------------------------------------
# Sysprep /generalize does NOT remove accounts that existed on the template
# (e.g. an interactive "rl" user). Enable the built-in Administrator and drop
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

# One-shot AutoLogon from unattend -- clear so later reboots stay at the logon screen.
try {
    $winlogon = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
    Set-ItemProperty -Path $winlogon -Name AutoAdminLogon -Value '0' -ErrorAction SilentlyContinue
    Remove-ItemProperty -Path $winlogon -Name DefaultPassword -ErrorAction SilentlyContinue
} catch {}

{% if join_domain %}
# --- Domain join -----------------------------------------------------------
# Credentials arrive as Base64(JSON{domain,username,password[,ou]}) so nothing
# sensitive is interpolated into PowerShell syntax and the password is never
# written to the log.
$blob = '{{ domain_join_b64 }}'
$j = ConvertFrom-Json ([System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($blob)))
$sec = ConvertTo-SecureString $j.password -AsPlainText -Force
$cred = New-Object System.Management.Automation.PSCredential($j.username, $sec)

# Wait for the network/DNS to be usable, then join with a few retries.
$joined = $false
for ($i = 0; $i -lt 10 -and -not $joined; $i++) {
    try {
        {% if domain_ou %}
        Add-Computer -DomainName $j.domain -OUPath $j.ou -Credential $cred -Force -ErrorAction Stop
        {% else %}
        Add-Computer -DomainName $j.domain -Credential $cred -Force -ErrorAction Stop
        {% endif %}
        $joined = $true
        Write-Output "setup.ps1: Joined domain $($j.domain)."
    } catch {
        Write-Output "setup.ps1: Domain join attempt $($i + 1) failed: $($_.Exception.Message)"
        Start-Sleep -Seconds 15
    }
}

# Scrub the credential-bearing script from disk regardless of outcome.
Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue

# Mark complete before reboot so FirstLogonCommands / SetupComplete re-entry is a no-op.
'ok' | Set-Content -Path 'C:\ProgramData\GuestOS\setup.done' -Encoding ASCII

if ($joined) {
    Write-Output "setup.ps1: Restarting to finalize domain membership."
    try { Stop-Transcript | Out-Null } catch {}
    shutdown /r /t 5
} elseif ($pagefileLetter) {
    Write-Output "setup.ps1: Restarting so pagefile on ${pagefileLetter}: becomes active."
    try { Stop-Transcript | Out-Null } catch {}
    shutdown /r /t 5
} else {
    Write-Output "setup.ps1: Domain join FAILED after retries; leaving machine in workgroup."
    try { Stop-Transcript | Out-Null } catch {}
}
{% else %}
# Mark complete before reboot so FirstLogonCommands / SetupComplete re-entry is a no-op.
'ok' | Set-Content -Path 'C:\ProgramData\GuestOS\setup.done' -Encoding ASCII

if ($pagefileLetter) {
    Write-Output "setup.ps1: Restarting so pagefile on ${pagefileLetter}: becomes active."
    try { Stop-Transcript | Out-Null } catch {}
    shutdown /r /t 5
} else {
    try { Stop-Transcript | Out-Null } catch {}
}
{% endif %}
