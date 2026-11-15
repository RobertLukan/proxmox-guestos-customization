"""Clone an OPNsense template onto two untagged NICs and enable outbound NAT."""
from __future__ import annotations

from app.validators import ValidationError, validate_bridge, validate_ipv4, validate_netmask, validate_vlan


def prepare_appliance_payload(data):
    """Require a WAN NIC and a LAN NIC, both without a VLAN tag."""
    nics = data.get('nics')
    if not isinstance(nics, list) or len(nics) != 2:
        raise ValidationError('An appliance needs a WAN NIC and a LAN NIC.')
    roles = []
    cleaned = []
    for index, raw in enumerate(nics):
        if not isinstance(raw, dict):
            raise ValidationError(f'nics[{index}] must be an object.')
        if validate_vlan(raw.get('vlan')) is not None:
            raise ValidationError('Appliance NICs cannot use a VLAN on this bridge.')
        role = str(raw.get('role') or '').strip().lower()
        if role not in {'wan', 'lan'}:
            raise ValidationError(f'nics[{index}].role must be wan or lan.')
        bridge = validate_bridge(raw.get('bridge'))
        if not bridge:
            raise ValidationError(f'nics[{index}].bridge is required.')
        mode = str(raw.get('network_mode') or 'static').strip().lower()
        if mode != 'static':
            raise ValidationError('Appliance NICs use a static address.')
        address = validate_ipv4(raw.get('ip_address'), f'nics[{index}].ip_address')
        prefix = validate_netmask(raw.get('netmask'))
        gateway = ''
        if role == 'wan':
            gateway = validate_ipv4(raw.get('gateway'), f'nics[{index}].gateway')
        cleaned.append({
            'role': role,
            'bridge': bridge,
            'vlan': None,
            'network_mode': 'static',
            'ip_address': address,
            'netmask': prefix,
            'gateway': gateway,
        })
        roles.append(role)
    if sorted(roles) != ['lan', 'wan']:
        raise ValidationError('The appliance needs one WAN NIC and one LAN NIC.')
    # Always clone WAN as net0 / vtnet0 and LAN as net1 / vtnet1, regardless of
    # the request order (OPNsense setup script hard-maps those interfaces).
    by_role = {nic['role']: nic for nic in cleaned}
    data['nics'] = [by_role['wan'], by_role['lan']]
    data['nat'] = True
    data['bridge'] = data['nics'][0]['bridge']
    data['vlan'] = None
    return data


def opnsense_setup_script(nics):
    """Shell script that sets WAN, LAN, and automatic outbound NAT.

    Addresses are validated before they are placed in the script.
    """
    wan = next(nic for nic in nics if nic['role'] == 'wan')
    lan = next(nic for nic in nics if nic['role'] == 'lan')
    return f"""set -eu
if [ ! -f /usr/local/etc/inc/config.inc ]; then
  echo "OPNsense config.inc is not on this template" >&2
  exit 1
fi
php <<'PHP'
<?php
require_once('/usr/local/etc/inc/config.inc');
require_once('/usr/local/etc/inc/util.inc');
$config['interfaces']['wan']['if'] = 'vtnet0';
$config['interfaces']['wan']['enable'] = '1';
$config['interfaces']['wan']['ipaddr'] = '{wan['ip_address']}';
$config['interfaces']['wan']['subnet'] = '{wan['netmask']}';
$config['interfaces']['wan']['gateway'] = 'WANGW';
$config['interfaces']['lan']['if'] = 'vtnet1';
$config['interfaces']['lan']['enable'] = '1';
$config['interfaces']['lan']['ipaddr'] = '{lan['ip_address']}';
$config['interfaces']['lan']['subnet'] = '{lan['netmask']}';
if (!isset($config['gateways']) || !is_array($config['gateways'])) {{
    $config['gateways'] = [];
}}
$config['gateways']['gateway_item'] = [[
    'interface' => 'wan',
    'gateway' => '{wan['gateway']}',
    'name' => 'WANGW',
    'ipprotocol' => 'inet',
    'descr' => 'WAN gateway',
]];
if (!isset($config['nat']) || !is_array($config['nat'])) {{
    $config['nat'] = [];
}}
if (!isset($config['nat']['outbound']) || !is_array($config['nat']['outbound'])) {{
    $config['nat']['outbound'] = [];
}}
$config['nat']['outbound']['mode'] = 'automatic';
write_config('GuestOS appliance network');
PHP
/usr/local/sbin/configctl interface reconfigure wan
/usr/local/sbin/configctl interface reconfigure lan
/usr/local/sbin/configctl filter reload
"""
