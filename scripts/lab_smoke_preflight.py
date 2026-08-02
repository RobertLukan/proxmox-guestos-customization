#!/usr/bin/env python3
"""Lab smoke preflight: ensure Proxmox has enough free RAM before cloning.

Used by ``lab_*_smoke.py`` scripts. Talks to the PVE API with credentials from
the GuestOS ``.env`` (``PROXMOX_HOST`` / ``PROXMOX_USER`` / ``PROXMOX_PASSWORD``)
via stdlib ``urllib`` — no Flask/proxmoxer required on the host.

Formula (MiB):

  need = (ram_mb_per_vm * vm_count) + reserve_mb
  free = sum over nodes of (maxmem - mem)

Default reserve is 4096 MiB so the hypervisor / other guests have headroom.
Override with ``LAB_SMOKE_RAM_RESERVE_MB`` or ``--ram-reserve-mb``.
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request


def _repo_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def _parse_dotenv(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if not os.path.isfile(path):
        return out
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, val = line.partition('=')
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key:
                out[key] = val
    return out


def _pve_creds() -> tuple[str, str, str, bool]:
    """Return (host, user, password, verify_ssl)."""
    env = dict(os.environ)
    dotenv = _parse_dotenv(os.path.join(_repo_root(), '.env'))
    for k, v in dotenv.items():
        env.setdefault(k, v)

    host = (env.get('PROXMOX_HOST') or '').strip()
    user = (env.get('PROXMOX_USER') or '').strip()
    password = env.get('PROXMOX_PASSWORD') or ''
    verify_raw = (env.get('PROXMOX_VERIFY_SSL') or 'false').strip().lower()
    verify_ssl = verify_raw in ('1', 'true', 'yes')
    if not host or not user or not password:
        raise RuntimeError(
            'PROXMOX_HOST / PROXMOX_USER / PROXMOX_PASSWORD missing '
            '(set in environment or GuestOS .env)'
        )
    return host, user, password, verify_ssl


def _ssl_context(verify_ssl: bool):
    if verify_ssl:
        return None
    return ssl._create_unverified_context()


def _pve_json(method: str, url: str, headers: dict, data=None, verify_ssl=False):
    body = None
    hdrs = dict(headers)
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        hdrs.setdefault('Content-Type', 'application/x-www-form-urlencoded')
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(
            req, timeout=30, context=_ssl_context(verify_ssl)
        ) as resp:
            raw = resp.read().decode()
            payload = json.loads(raw) if raw else {}
            return resp.status, payload
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8', errors='replace')
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {'error': raw}
        return e.code, payload


def _pve_session():
    host, user, password, verify_ssl = _pve_creds()
    base = f'https://{host}:8006'
    code, ticket = _pve_json(
        'POST',
        f'{base}/api2/json/access/ticket',
        {},
        data={'username': user, 'password': password},
        verify_ssl=verify_ssl,
    )
    if code != 200:
        raise RuntimeError(f'PVE ticket failed HTTP {code}: {ticket}')
    data = ticket.get('data') or {}
    ticket_val = data.get('ticket')
    csrf = data.get('CSRFPreventionToken')
    if not ticket_val:
        raise RuntimeError(f'PVE ticket response missing ticket: {ticket}')
    headers = {
        'Cookie': f'PVEAuthCookie={ticket_val}',
        'CSRFPreventionToken': csrf or '',
    }
    return base, headers, verify_ssl


def collect_node_memory():
    """Return (nodes, running_vms) from PVE cluster.resources.

    Each node: ``{name, maxmem_mb, used_mb, free_mb}``.
    Each running VM: ``{vmid, name, node, maxmem_mb, mem_mb}``.
    """
    base, headers, verify_ssl = _pve_session()
    code, payload = _pve_json(
        'GET',
        f'{base}/api2/json/cluster/resources',
        headers,
        verify_ssl=verify_ssl,
    )
    if code != 200:
        raise RuntimeError(f'cluster/resources failed HTTP {code}: {payload}')
    resources = payload.get('data') or []

    nodes = []
    running = []
    for r in resources:
        rtype = r.get('type')
        if rtype == 'node':
            maxmem = int(r.get('maxmem') or 0)
            used = int(r.get('mem') or 0)
            nodes.append(
                {
                    'name': r.get('node') or r.get('id') or '?',
                    'maxmem_mb': maxmem // (1024 * 1024),
                    'used_mb': used // (1024 * 1024),
                    'free_mb': max(0, (maxmem - used) // (1024 * 1024)),
                }
            )
        elif rtype == 'qemu':
            if int(r.get('template') or 0):
                continue
            if (r.get('status') or '') != 'running':
                continue
            maxmem = int(r.get('maxmem') or 0)
            mem = int(r.get('mem') or 0)
            running.append(
                {
                    'vmid': int(r.get('vmid') or 0),
                    'name': r.get('name') or '',
                    'node': r.get('node') or '',
                    'maxmem_mb': maxmem // (1024 * 1024),
                    'mem_mb': mem // (1024 * 1024),
                }
            )
    running.sort(key=lambda x: (-x['maxmem_mb'], x['vmid']))
    return nodes, running


def default_reserve_mb() -> int:
    raw = (os.environ.get('LAB_SMOKE_RAM_RESERVE_MB') or '4096').strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 4096


def check_lab_ram(
    ram_mb_per_vm: int,
    vm_count: int = 1,
    reserve_mb: int | None = None,
) -> tuple[bool, str]:
    """Return ``(ok, human_message)``. Does not exit."""
    if ram_mb_per_vm < 1 or vm_count < 1:
        return False, 'ram_mb_per_vm and vm_count must be >= 1'
    if reserve_mb is None:
        reserve_mb = default_reserve_mb()

    nodes, running = collect_node_memory()
    if not nodes:
        return False, 'No Proxmox nodes found in cluster.resources'

    free_total = sum(n['free_mb'] for n in nodes)
    need = (int(ram_mb_per_vm) * int(vm_count)) + int(reserve_mb)

    lines = [
        f'PVE RAM preflight: need {need} MiB '
        f'({vm_count}×{ram_mb_per_vm} MiB guests + {reserve_mb} MiB reserve); '
        f'free {free_total} MiB across {len(nodes)} node(s)',
    ]
    for n in nodes:
        lines.append(
            f"  node {n['name']}: "
            f"{n['used_mb']}/{n['maxmem_mb']} MiB used, "
            f"{n['free_mb']} MiB free"
        )
    if running:
        lines.append(f'  running VMs ({len(running)}):')
        for r in running[:25]:
            lines.append(
                f"    {r['vmid']} {r['name'] or '?'} "
                f"@{r['node']} max={r['maxmem_mb']} MiB "
                f"rss~{r['mem_mb']} MiB"
            )
        if len(running) > 25:
            lines.append(f'    … +{len(running) - 25} more')
    else:
        lines.append('  running VMs: (none)')

    ok = free_total >= need
    if ok:
        lines.append(f'OK   RAM headroom {free_total - need} MiB')
    else:
        lines.append(
            f'FAIL not enough free RAM '
            f'(short {need - free_total} MiB). '
            f'Stop unused lab VMs or pass --skip-ram-check '
            f'(not recommended).'
        )
    return ok, '\n'.join(lines)


def ensure_lab_ram(
    ram_mb_per_vm: int,
    vm_count: int = 1,
    reserve_mb: int | None = None,
    skip: bool = False,
) -> int:
    """Print preflight; return 0 if ok, 1 if insufficient / error.

    When ``skip`` is True, still prints a warning and returns 0.
    """
    if skip:
        print(
            'WARN skipping PVE RAM preflight (--skip-ram-check). '
            f'Would have requested {vm_count}×{ram_mb_per_vm} MiB.'
        )
        return 0
    try:
        ok, msg = check_lab_ram(ram_mb_per_vm, vm_count, reserve_mb=reserve_mb)
    except Exception as e:
        print(
            f'FAIL PVE RAM preflight error: {e}\n'
            '     Need PROXMOX_* in GuestOS .env (lab host), '
            'or pass --skip-ram-check.',
            file=sys.stderr,
        )
        return 1
    print(msg)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--ram', type=int, default=4096, help='MiB per guest VM')
    p.add_argument('--count', type=int, default=1, help='Number of guests to start')
    p.add_argument(
        '--reserve-mb',
        type=int,
        default=None,
        help='Host headroom MiB (default LAB_SMOKE_RAM_RESERVE_MB or 4096)',
    )
    args = p.parse_args(argv)
    return ensure_lab_ram(args.ram, args.count, reserve_mb=args.reserve_mb)


if __name__ == '__main__':
    raise SystemExit(main())
