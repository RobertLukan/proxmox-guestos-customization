"""Linux cloud-init customize: validate payload, pack nics, render vendor-data."""
from __future__ import annotations

import base64

from app.linux_disks import prepare_linux_disk_plan
from app.util import as_bool
from app.validators import (
    ValidationError,
    validate_dns_servers,
    validate_ipv4,
    validate_ipv6,
    validate_ipv6_prefix,
    validate_linux_hostname,
    validate_netmask,
    validate_vlan,
)


def _normalize_nic(raw, index, defaults):
    if not isinstance(raw, dict):
        raise ValidationError(f'nics[{index}] must be an object.')
    nic = {}
    nic['bridge'] = (raw.get('bridge') or defaults.get('bridge') or 'vmbr0').strip()
    nic['vlan'] = validate_vlan(raw.get('vlan', defaults.get('vlan')))
    mode = str(raw.get('network_mode') or defaults.get('network_mode') or 'dhcp').strip().lower()
    if mode not in ('dhcp', 'static'):
        raise ValidationError(f'nics[{index}].network_mode must be dhcp or static.')
    nic['network_mode'] = mode
    if mode == 'static':
        nic['ip_address'] = validate_ipv4(raw.get('ip_address'), field=f'NIC{index} IP')
        # UI / Windows collectors use netmask_cidr; accept both.
        nic['netmask'] = validate_netmask(raw.get('netmask', raw.get('netmask_cidr')))
        gw = raw.get('gateway') or defaults.get('gateway')
        nic['gateway'] = validate_ipv4(gw, field=f'NIC{index} gateway') if gw else ''
    else:
        nic['ip_address'] = ''
        nic['netmask'] = ''
        nic['gateway'] = ''

    enable_v6 = as_bool(raw.get('enable_ipv6'), False)
    nic['enable_ipv6'] = enable_v6
    if enable_v6:
        v6_mode = str(raw.get('ipv6_mode') or 'static').strip().lower()
        if v6_mode not in ('static', 'dhcp'):
            raise ValidationError(f'nics[{index}].ipv6_mode must be static or dhcp.')
        nic['ipv6_mode'] = v6_mode
        if v6_mode == 'static':
            nic['ipv6_address'] = validate_ipv6(
                raw.get('ipv6_address'), field=f'NIC{index} IPv6 address'
            )
            nic['ipv6_prefix'] = validate_ipv6_prefix(raw.get('ipv6_prefix') or 64)
            gw6 = (raw.get('ipv6_gateway') or '').strip()
            nic['ipv6_gateway'] = (
                validate_ipv6(gw6, field=f'NIC{index} IPv6 gateway') if gw6 else ''
            )
        else:
            nic['ipv6_address'] = ''
            nic['ipv6_prefix'] = 64
            nic['ipv6_gateway'] = ''
    else:
        nic['ipv6_mode'] = ''
        nic['ipv6_address'] = ''
        nic['ipv6_prefix'] = 64
        nic['ipv6_gateway'] = ''
    return nic


def prepare_linux_payload(data):
    """Validate and normalize a Linux cloud-init customize request in place."""
    data['hostname'] = validate_linux_hostname(data.get('hostname'))
    try:
        data['cores'] = int(data.get('cores'))
        data['ram'] = int(data.get('ram'))
    except (TypeError, ValueError):
        raise ValidationError('cores and ram must be integers.')

    bridge = (data.get('bridge') or '').strip() or 'vmbr0'
    data['bridge'] = bridge
    data['vlan'] = validate_vlan(data.get('vlan'))

    mode = str(data.get('network_mode') or 'dhcp').strip().lower()
    if mode not in ('dhcp', 'static'):
        raise ValidationError('network_mode must be dhcp or static.')
    data['network_mode'] = mode
    data['use_dhcp'] = mode == 'dhcp'

    defaults = {
        'bridge': bridge,
        'vlan': data.get('vlan'),
        'network_mode': mode,
        'gateway': data.get('gateway'),
    }

    raw_nics = data.get('nics')
    if isinstance(raw_nics, list) and raw_nics:
        data['nics'] = [_normalize_nic(n, i, defaults) for i, n in enumerate(raw_nics)]
    else:
        primary = {
            'bridge': bridge,
            'vlan': data.get('vlan'),
            'network_mode': mode,
            'ip_address': data.get('ip_address'),
            'netmask': data.get('netmask'),
            'gateway': data.get('gateway'),
            'enable_ipv6': data.get('enable_ipv6'),
            'ipv6_mode': data.get('ipv6_mode'),
            'ipv6_address': data.get('ipv6_address'),
            'ipv6_prefix': data.get('ipv6_prefix'),
            'ipv6_gateway': data.get('ipv6_gateway'),
        }
        data['nics'] = [_normalize_nic(primary, 0, defaults)]

    # Mirror primary onto top-level for verify/task ledger.
    primary = data['nics'][0]
    data['network_mode'] = primary['network_mode']
    data['use_dhcp'] = primary['network_mode'] == 'dhcp'
    data['ip_address'] = primary.get('ip_address') or ''
    data['netmask'] = primary.get('netmask') or ''
    data['gateway'] = primary.get('gateway') or ''
    data['enable_ipv6'] = bool(primary.get('enable_ipv6'))
    data['ipv6_address'] = primary.get('ipv6_address') or ''
    data['ipv6_prefix'] = primary.get('ipv6_prefix') or 64
    data['ipv6_gateway'] = primary.get('ipv6_gateway') or ''

    dns = validate_dns_servers(data.get('dns_servers'), allow_ipv6=True)
    data['dns_servers'] = ','.join(dns) if dns else ''
    data['dns_list'] = dns
    data['searchdomain'] = (str(data.get('searchdomain') or '').strip() or None)

    data['ciuser'] = (str(data.get('ciuser') or '').strip() or None)
    data['sshkeys'] = (str(data.get('sshkeys') or '').strip() or None)
    # Prefer SSH keys; password optional.
    data['cipassword'] = data.get('cipassword') or None

    data['detach_cloudinit_after_ready'] = as_bool(
        data.get('detach_cloudinit_after_ready'), False
    )

    _apply_os_disk_gb(data)
    prepare_linux_disk_plan(data)
    return data


def _apply_os_disk_gb(data):
    """Map simple ``os_disk_gb`` into a manage_disks OS grow plan when set."""
    from app.linux_disks import _int_gb

    raw = data.get('os_disk_gb')
    if raw in (None, ''):
        data['os_disk_gb'] = None
        return
    gb = _int_gb(raw, 'os_disk_gb')
    data['os_disk_gb'] = gb
    data['manage_disks'] = True
    disks = data.get('disks')
    if not isinstance(disks, list) or not disks:
        data['disks'] = [{'role': 'os', 'grow_to_gb': gb}]
        return
    for entry in disks:
        if not isinstance(entry, dict):
            continue
        if str(entry.get('role') or '').strip().lower() == 'os':
            if entry.get('grow_to_gb') in (None, ''):
                entry['grow_to_gb'] = gb
            return
    disks.insert(0, {'role': 'os', 'grow_to_gb': gb})


def render_linux_vendor_data(data, disk_guest_plan=None):
    """Render cloud-init vendor-data YAML for optional disk mounts (advanced).

    Simple large-root path returns empty string (PVE ipconfig/ciuser enough).
    """
    plan = disk_guest_plan or data.get('disk_guest_plan') or []
    data_disks = [d for d in plan if d.get('role') == 'data']
    swap_disks = [d for d in plan if d.get('role') == 'swap']
    if not data_disks and not swap_disks:
        return ''

    lines = [
        '#cloud-config',
        '# GuestOS Linux advanced disk layout',
        'disk_setup:',
    ]
    for d in data_disks + swap_disks:
        serial = d.get('serial')
        lines.append(f'  /dev/disk/by-id/scsi-{serial}:')
        lines.append('    table_type: gpt')
        lines.append('    layout: true')
        lines.append('    overwrite: true')

    lines.append('fs_setup:')
    for d in data_disks:
        serial = d.get('serial')
        fstype = d.get('fstype') or 'ext4'
        lines.append(f'  - label: {serial}')
        lines.append(f'    filesystem: {fstype}')
        lines.append(f'    device: /dev/disk/by-id/scsi-{serial}')
        lines.append('    partition: auto')
    for d in swap_disks:
        serial = d.get('serial')
        lines.append(f'  - label: {serial}')
        lines.append('    filesystem: swap')
        lines.append(f'    device: /dev/disk/by-id/scsi-{serial}')
        lines.append('    partition: auto')

    lines.append('mounts:')
    for d in data_disks:
        serial = d.get('serial')
        mp = d.get('mountpoint') or '/data'
        fstype = d.get('fstype') or 'ext4'
        lines.append(f'  - ["LABEL={serial}", "{mp}", "{fstype}", "defaults,nofail", "0", "2"]')
    for d in swap_disks:
        serial = d.get('serial')
        lines.append(f'  - ["LABEL={serial}", "none", "swap", "sw,nofail", "0", "0"]')

    return '\n'.join(lines) + '\n'


def vendor_data_b64(data, disk_guest_plan=None):
    text = render_linux_vendor_data(data, disk_guest_plan=disk_guest_plan)
    if not text:
        return ''
    return base64.b64encode(text.encode('utf-8')).decode('ascii')


def clone_nic_list(data):
    """Bridge/vlan only list for clone_vm."""
    out = []
    for nic in data.get('nics') or []:
        out.append({'bridge': nic.get('bridge'), 'vlan': nic.get('vlan')})
    return out or None
