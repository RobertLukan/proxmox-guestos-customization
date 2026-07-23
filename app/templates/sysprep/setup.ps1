# Static network configuration applied after Sysprep by SetupComplete.cmd.
#
# NOTE: this file is NOT autoescaped by Jinja (it is not HTML/XML), so every
# interpolated value below is validated server-side in _validate_sysprep_network
# (IPv4 addresses, an integer prefix length, a DNS list, and a MAC address)
# before this template is rendered. Do not add unvalidated fields.

$ErrorActionPreference = 'Stop'

$mac     = '{{ primary_mac_address | default("", true) }}'.Replace(':', '-').ToUpper()
$ip      = '{{ ip_address }}'
$prefix  = {{ netmask_cidr }}
$gateway = '{{ gateway }}'
$dns     = @({% for d in dns_list %}'{{ d }}'{% if not loop.last %}, {% endif %}{% endfor %})

Write-Output "[setup.ps1] Requested MAC=$mac IP=$ip/$prefix GW=$gateway DNS=$($dns -join ',')"

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

# Remove any existing IPv4 address / default route so re-runs are idempotent.
Remove-NetIPAddress -InterfaceIndex $ifIndex -AddressFamily IPv4 -Confirm:$false -ErrorAction SilentlyContinue
Remove-NetRoute     -InterfaceIndex $ifIndex -AddressFamily IPv4 -Confirm:$false -ErrorAction SilentlyContinue

# Disable DHCP and apply the static address + default gateway.
Set-NetIPInterface -InterfaceIndex $ifIndex -Dhcp Disabled
New-NetIPAddress -InterfaceIndex $ifIndex -IPAddress $ip -PrefixLength $prefix -DefaultGateway $gateway | Out-Null

# Apply DNS servers (or reset to DHCP-provided if none were supplied).
if ($dns.Count -gt 0) {
    Set-DnsClientServerAddress -InterfaceIndex $ifIndex -ServerAddresses $dns
} else {
    Set-DnsClientServerAddress -InterfaceIndex $ifIndex -ResetServerAddresses
}

Write-Output "[setup.ps1] Network configuration complete."
