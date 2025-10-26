$ip = '{{ ip_address }}'
$netmask = '{{ netmask_cidr }}'
$gateway = '{{ gateway }}'
$dns1 = '{{ dns_servers.split(',')[0] }}'
$dns2 = '{{ dns_servers.split(',')[1] if dns_servers.split(',')|length > 1 else '' }}'

Get-NetAdapter | Where-Object {$_.Status -eq 'Up'} | New-NetIPAddress -IPAddress $ip -PrefixLength $netmask -DefaultGateway $gateway
Set-DnsClientServerAddress -InterfaceIndex (Get-NetAdapter | Where-Object {$_.Status -eq 'Up'}).InterfaceIndex -ServerAddresses ($dns1, $dns2)
