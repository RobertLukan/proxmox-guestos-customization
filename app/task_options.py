"""Sanitize sysprep request options for durable job history (no secrets)."""
from __future__ import annotations

import json

_SECRET_KEYS = frozenset({
    'administrator_password',
    'domain_password',
    'domain_join_b64',
    'cipassword',
    'sshkeys',
    '_pve',
    'password',
})


def build_task_options(data: dict | None) -> dict:
    """Return a compact, secret-free snapshot of customization choices."""
    raw = dict(data or {})
    out = {}

    def _set(key, value):
        if value in (None, '', [], {}):
            return
        out[key] = value

    _set('network_mode', (raw.get('network_mode') or '').strip().lower() or None)
    _set('bridge', (raw.get('bridge') or '').strip() or None)
    vlan = raw.get('vlan')
    if vlan not in (None, '', 'None'):
        try:
            out['vlan'] = int(vlan)
        except (TypeError, ValueError):
            out['vlan'] = str(vlan)
    _set('dns_servers', (raw.get('dns_servers') or '').strip() or None)
    _set('gateway', (raw.get('gateway') or '').strip() or None)
    join = raw.get('join_domain')
    if join is not None:
        out['join_domain'] = bool(join) if not isinstance(join, str) else join.lower() in (
            '1', 'true', 'yes', 'on',
        )
    _set('domain_name', (raw.get('domain_name') or '').strip() or None)
    _set('domain_username', (raw.get('domain_username') or '').strip() or None)
    _set('domain_profile', (raw.get('domain_profile') or '').strip() or None)
    if 'use_domain_profile_credentials' in raw:
        upc = raw.get('use_domain_profile_credentials')
        if isinstance(upc, str):
            out['use_domain_profile_credentials'] = upc.lower() in ('1', 'true', 'yes', 'on')
        else:
            out['use_domain_profile_credentials'] = bool(upc)
    manage = raw.get('manage_disks')
    if manage is not None:
        if isinstance(manage, str):
            out['manage_disks'] = manage.lower() in ('1', 'true', 'yes', 'on')
        else:
            out['manage_disks'] = bool(manage)
    disks = raw.get('disks')
    if isinstance(disks, list) and disks:
        slim = []
        for d in disks:
            if not isinstance(d, dict):
                continue
            entry = {'role': d.get('role') or 'data'}
            for k in ('size_gb', 'grow_to_gb', 'letter', 'mountpoint', 'fstype'):
                if d.get(k) not in (None, ''):
                    entry[k] = d.get(k)
            slim.append(entry)
        if slim:
            out['disks'] = slim
    nics = raw.get('nics')
    if isinstance(nics, list) and nics:
        out['nic_count'] = len(nics)
    elif out.get('network_mode'):
        out['nic_count'] = 1
    for key in (
        'cores', 'ram', 'spec_id', 'timezone', 'locale', 'workgroup',
        'ciuser', 'searchdomain', 'os_family', 'os_disk_gb',
    ):
        if raw.get(key) not in (None, ''):
            out[key] = raw.get(key)
    if raw.get('detach_cloudinit_after_ready') not in (None, '', False):
        dci = raw.get('detach_cloudinit_after_ready')
        if isinstance(dci, str):
            out['detach_cloudinit_after_ready'] = dci.lower() in ('1', 'true', 'yes', 'on')
        else:
            out['detach_cloudinit_after_ready'] = bool(dci)
    if raw.get('fast_waits') not in (None, '', False):
        fw = raw.get('fast_waits')
        if isinstance(fw, str):
            out['fast_waits'] = fw.lower() in ('1', 'true', 'yes', 'on')
        else:
            out['fast_waits'] = bool(fw)
    if raw.get('enable_ipv6') not in (None, '', False):
        v6 = raw.get('enable_ipv6')
        if isinstance(v6, str):
            out['enable_ipv6'] = v6.lower() in ('1', 'true', 'yes', 'on')
        else:
            out['enable_ipv6'] = bool(v6)
    if raw.get('product_key'):
        out['product_key_set'] = True
    return out


def options_to_json(data: dict | None) -> str:
    return json.dumps(build_task_options(data), separators=(',', ':'), sort_keys=True)


def options_summary_chips(options: dict | None) -> list[str]:
    """Short labels for Jobs table chips."""
    o = options or {}
    chips = []
    mode = (o.get('network_mode') or '').lower()
    if mode:
        chips.append(mode.upper() if mode == 'dhcp' else 'static')
    if o.get('join_domain'):
        chips.append('AD')
    elif o.get('workgroup'):
        chips.append('WG')
    if o.get('os_family') == 'linux':
        chips.append('Linux')
    if o.get('os_disk_gb'):
        chips.append(f"{o['os_disk_gb']}G")
    if o.get('detach_cloudinit_after_ready'):
        chips.append('freeze')
    if o.get('manage_disks') and o.get('disks'):
        chips.append('disks')
    nic_count = o.get('nic_count') or 0
    if nic_count > 1:
        chips.append(f'{nic_count}NIC')
    if o.get('enable_ipv6'):
        chips.append('IPv6')
    if o.get('vlan') is not None:
        chips.append(f"VLAN{o['vlan']}")
    if o.get('spec_id'):
        chips.append('spec')
    return chips or ['—']


def parse_options_json(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}
