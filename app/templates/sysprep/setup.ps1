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

New-Item -ItemType Directory -Force -Path 'C:\ProgramData\GuestOS' | Out-Null
try {
    Start-Transcript -Path 'C:\ProgramData\GuestOS\setup.log' -Append | Out-Null
} catch {}

$mac = '{{ primary_mac_address | default("", true) }}'.Replace(':', '-').ToUpper()

# Select the target adapter by MAC; fall back to the first physical adapter so a
# single-NIC VM still configures correctly even if the MAC could not be resolved.
$adapter = $null
if ($mac) {
    $adapter = Get-NetAdapter -Physical | Where-Object { $_.MacAddress.ToUpper() -eq $mac } | Select-Object -First 1
}
if (-not $adapter) {
    Write-Output "[setup.ps1] MAC '$mac' not matched; falling back to first physical adapter."
    $adapter = Get-NetAdapter -Physical | Sort-Object ifIndex | Select-Object -First 1
}
if (-not $adapter) { throw "No physical network adapter found." }

$ifIndex = $adapter.ifIndex
Write-Output "[setup.ps1] Using adapter '$($adapter.Name)' (ifIndex $ifIndex, MAC $($adapter.MacAddress))."

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
Write-Output "[setup.ps1] Enabling DHCP on ifIndex $ifIndex."
Set-NetIPInterface -InterfaceIndex $ifIndex -Dhcp Enabled
{% if dns_list %}
# Explicit DNS override (e.g. to reach the domain controller) even under DHCP.
Set-DnsClientServerAddress -InterfaceIndex $ifIndex -ServerAddresses @({% for d in dns_list %}'{{ d }}'{% if not loop.last %}, {% endif %}{% endfor %})
{% else %}
Set-DnsClientServerAddress -InterfaceIndex $ifIndex -ResetServerAddresses
{% endif %}
# Clearing addresses above drops the lease; renew so the guest is not left without IPv4.
Write-Output "[setup.ps1] Renewing DHCP lease on '$($adapter.Name)'."
cmd /c "ipconfig /renew `"$($adapter.Name)`"" 2>&1 | Out-String | Write-Output
for ($i = 0; $i -lt 12; $i++) {
    $lease = Get-NetIPAddress -InterfaceIndex $ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notlike '169.254.*' -and $_.IPAddress -notlike '127.*' } |
        Select-Object -First 1
    if ($lease) {
        Write-Output "[setup.ps1] DHCP lease: $($lease.IPAddress)"
        break
    }
    Start-Sleep -Seconds 5
}
if (-not $lease) {
    Write-Output "[setup.ps1] WARNING: no DHCP lease yet; Windows may still be acquiring one."
}
{% else %}
$ip      = '{{ ip_address }}'
$prefix  = {{ netmask_cidr }}
$gateway = '{{ gateway }}'
$dns     = @({% for d in dns_list %}'{{ d }}'{% if not loop.last %}, {% endif %}{% endfor %})
Write-Output "[setup.ps1] Static IP=$ip/$prefix GW=$gateway DNS=$($dns -join ',')"
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
        Write-Output "[setup.ps1] Static addressing applied."
    } catch {
        Write-Output "[setup.ps1] Static IP attempt $($i + 1) failed: $($_.Exception.Message)"
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

Write-Output "[setup.ps1] Network configuration complete."

# --- Local accounts --------------------------------------------------------
# Sysprep /generalize does NOT remove accounts that existed on the template
# (e.g. an interactive "rl" user). Enable the built-in Administrator and drop
# every other local account so clones boot to a clean admin-only local state.
# Built-in / system accounts are left alone.
Write-Output "[setup.ps1] Enabling built-in Administrator account."
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
    Write-Output "[setup.ps1] Removing leftover local user '$($_.Name)'."
    try {
        Remove-LocalUser -Name $_.Name -ErrorAction Stop
    } catch {
        Write-Output "[setup.ps1] Could not remove '$($_.Name)': $($_.Exception.Message)"
    }
}

# One-shot AutoLogon from unattend — clear so later reboots stay at the logon screen.
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
        Write-Output "[setup.ps1] Joined domain $($j.domain)."
    } catch {
        Write-Output "[setup.ps1] Domain join attempt $($i + 1) failed: $($_.Exception.Message)"
        Start-Sleep -Seconds 15
    }
}

# Scrub the credential-bearing script from disk regardless of outcome.
Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue

if ($joined) {
    Write-Output "[setup.ps1] Restarting to finalize domain membership."
    try { Stop-Transcript | Out-Null } catch {}
    shutdown /r /t 5
} else {
    Write-Output "[setup.ps1] Domain join FAILED after retries; leaving machine in workgroup."
    try { Stop-Transcript | Out-Null } catch {}
}
{% else %}
try { Stop-Transcript | Out-Null } catch {}
{% endif %}
