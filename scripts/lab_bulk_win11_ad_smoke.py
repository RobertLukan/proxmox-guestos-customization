#!/usr/bin/env python3
"""Lab: Win11 bulk DHCP + AD join smoke (2 VDIs by default).

Clones template 127 (windows11), DHCP, DNS pointing at the lab DC, joins
lab.test. Destructive — leaves result VMs unless you clean them up in PVE.

Defaults:

  template   127 (Win11-templ2)
  count      2
  DNS        192.168.123.191
  domain     lab.test
  join user  administrator@lab.test
  join pass  ChangeMe123!

Example (on the GuestOS lab host):

  export GUESTOS_API_TOKEN=...
  python3 scripts/lab_bulk_win11_ad_smoke.py --poll

Requires ALLOW_FAST_WAITS=true on the GuestOS instance if you want short waits
(request still sends fast_waits=true).
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request


def _ssl_context(insecure: bool):
    if insecure:
        return ssl._create_unverified_context()
    return None


def _req(method, url, token, body=None, insecure=False):
    data = None if body is None else json.dumps(body).encode()
    headers = {
        'Authorization': f'Bearer {token}',
        'Accept': 'application/json',
    }
    if body is not None:
        headers['Content-Type'] = 'application/json'
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    ctx = _ssl_context(insecure)
    try:
        with urllib.request.urlopen(request, timeout=90, context=ctx) as resp:
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8', errors='replace')
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {'error': raw}
        return e.code, payload


def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--base-url', default=os.environ.get('GUESTOS_URL', 'http://127.0.0.1:5001'))
    p.add_argument('--token', default=os.environ.get('GUESTOS_API_TOKEN', ''))
    p.add_argument('--insecure', action='store_true')
    p.add_argument('--template-vmid', type=int, default=127)
    p.add_argument('--count', type=int, default=2, help='Number of VDIs (default 2).')
    p.add_argument('--hostname-prefix', default='', help='Default: VDI + time suffix.')
    p.add_argument('--cores', type=int, default=2)
    p.add_argument('--ram', type=int, default=4096)
    p.add_argument('--bridge', default=os.environ.get('PRIMARY_BRIDGE', 'vmbr0'))
    p.add_argument('--dns-servers', default='192.168.123.191')
    p.add_argument('--domain-name', default='lab.test')
    p.add_argument('--domain-username', default='administrator@lab.test')
    p.add_argument('--domain-password', default='ChangeMe123!')
    p.add_argument('--domain-ou', default='')
    p.add_argument('--admin-password', default='ChangeMe123!')
    p.add_argument('--fast-waits', action='store_true', default=True)
    p.add_argument('--no-fast-waits', action='store_false', dest='fast_waits')
    p.add_argument('--poll', action='store_true', default=True)
    p.add_argument('--no-poll', action='store_false', dest='poll')
    p.add_argument('--poll-seconds', type=int, default=3600)
    p.add_argument(
        '--inspect-pve',
        action='store_true',
        help='After batch, print result VM tags via local app.proxmox (lab host).',
    )
    p.add_argument(
        '--ram-reserve-mb',
        type=int,
        default=None,
        help='Host RAM headroom for preflight (default 4096 or LAB_SMOKE_RAM_RESERVE_MB).',
    )
    p.add_argument(
        '--skip-ram-check',
        action='store_true',
        help='Skip PVE free-RAM preflight (not recommended; can OOM the lab).',
    )
    args = p.parse_args()

    if not args.token:
        print('GUESTOS_API_TOKEN / --token required', file=sys.stderr)
        return 1
    if args.count < 1 or args.count > 10:
        print('--count must be 1–10 (bulk max default)', file=sys.stderr)
        return 1

    # Preflight before any clone: fail closed if the lab is short on RAM.
    from lab_smoke_preflight import ensure_lab_ram

    rc = ensure_lab_ram(
        args.ram,
        args.count,
        reserve_mb=args.ram_reserve_mb,
        skip=args.skip_ram_check,
    )
    if rc != 0:
        return rc

    base = args.base_url.rstrip('/')
    insecure = args.insecure

    code, health = _req('GET', f'{base}/api/health', args.token, insecure=insecure)
    if code != 200 or health.get('status') != 'ok':
        print(f'FAIL health: HTTP {code} {health}', file=sys.stderr)
        return 1
    print(f'OK   health: {health}')

    code, ver = _req('GET', f'{base}/api/version', args.token, insecure=insecure)
    if code != 200:
        print(f'FAIL version: HTTP {code} {ver}', file=sys.stderr)
        return 1
    print(f'OK   version: {ver}')

    suffix = str(int(time.time()))[-6:]
    prefix = (args.hostname_prefix.strip() or f'VDI{suffix}')[:10]
    # NetBIOS-safe short names: PREFIX + A/B/...
    hosts = []
    for i in range(args.count):
        letter = chr(ord('A') + i) if i < 26 else str(i)
        hosts.append(f'{prefix}{letter}'[:15])

    shared = {
        'template_vmid': args.template_vmid,
        'cores': args.cores,
        'ram': args.ram,
        'bridge': args.bridge,
        'network_mode': 'dhcp',
        'dns_servers': args.dns_servers,
        'administrator_password': args.admin_password,
        'timezone': 'Central European Standard Time',
        'locale': 'en-US',
        'join_domain': True,
        'use_domain_profile_credentials': False,
        'domain_name': args.domain_name,
        'domain_username': args.domain_username,
        'domain_password': args.domain_password,
        'fast_waits': bool(args.fast_waits),
        'manage_disks': False,
    }
    if args.domain_ou.strip():
        shared['domain_ou'] = args.domain_ou.strip()

    body = {
        'request_id': f'bulk-win11-ad-{suffix}',
        'shared': shared,
        'items': [{'hostname': h} for h in hosts],
    }

    safe_shared = {k: v for k, v in shared.items() if k != 'domain_password'}
    safe_shared['domain_password'] = '***'
    print(
        '     bulk AD payload: '
        f"template={shared['template_vmid']} hosts={hosts} "
        f"dns={shared['dns_servers']} domain={shared['domain_name']} "
        f"user={shared['domain_username']} fast_waits={shared['fast_waits']}"
    )
    print(f'     shared: {json.dumps(safe_shared, separators=(",", ":"))}')

    code, batch = _req(
        'POST',
        f'{base}/start_sysprep_bulk_workflow',
        args.token,
        body=body,
        insecure=insecure,
    )
    print(
        'OK   start_bulk'
        if code == 200 and batch.get('batch_id')
        else 'FAIL start_bulk',
        code,
        {
            k: batch.get(k)
            for k in (
                'batch_id',
                'accepted_count',
                'rejected_count',
                'task_ids',
                'error',
                'errors',
                'warnings',
                'message',
            )
            if batch.get(k) is not None
        },
    )
    if code != 200 or not batch.get('batch_id'):
        return 2
    if int(batch.get('accepted_count') or 0) < len(hosts):
        print(
            f'FAIL accepted_count={batch.get("accepted_count")} '
            f'rejected={batch.get("rejected_count")}',
            file=sys.stderr,
        )
        return 2

    batch_id = batch['batch_id']
    if not args.poll:
        print(f'OK   batch_id={batch_id} (not polling)')
        return 0

    deadline = time.time() + max(60, args.poll_seconds)
    final_tasks = None
    while time.time() < deadline:
        code, st = _req(
            'GET', f'{base}/api/batches/{batch_id}', args.token, insecure=insecure
        )
        if code != 200:
            print(f'     batch_poll_fail HTTP {code} {st}')
            time.sleep(5)
            continue
        tasks = st.get('tasks') or []
        if not tasks:
            code2, tl = _req(
                'GET',
                f'{base}/api/tasks?batch_id={batch_id}',
                args.token,
                insecure=insecure,
            )
            tasks = (tl.get('tasks') if code2 == 200 else []) or []
        short = []
        for t in tasks:
            msg = (t.get('message') or '')[:80]
            short.append(
                f"{t.get('hostname')}:{t.get('status')}:{t.get('progress')}%{msg}"
            )
        print(
            '     batch',
            {
                k: st.get(k)
                for k in (
                    'status',
                    'accepted_items',
                    'succeeded_items',
                    'failed_items',
                    'running_items',
                )
            },
            '|',
            ' || '.join(short) or '(no tasks yet)',
        )
        statuses = [t.get('status') for t in tasks]
        if len(tasks) >= len(hosts) and all(
            s in ('SUCCESS', 'FAILURE', 'CANCELLED') for s in statuses
        ):
            final_tasks = tasks
            break
        time.sleep(10)
    else:
        print('FAIL TIMEOUT waiting for bulk batch', file=sys.stderr)
        return 3

    ok = len(final_tasks) == len(hosts) and all(
        t.get('status') == 'SUCCESS' for t in final_tasks
    )
    print('RESULT', 'PASS' if ok else 'FAIL')
    for t in final_tasks:
        print(
            '---',
            t.get('hostname'),
            t.get('status'),
            'vmid=',
            t.get('result_vmid'),
        )
        print((t.get('message') or '')[:300])

    if args.inspect_pve:
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        from app import create_app
        from app.proxmox import _get_vm_node, get_proxmox_api

        app = create_app()
        with app.app_context():
            px = get_proxmox_api()
            for t in final_tasks:
                vmid = t.get('result_vmid')
                if not vmid:
                    continue
                node = _get_vm_node(int(vmid))
                cfg = px.nodes(node).qemu(int(vmid)).config.get()
                print('pve', vmid, 'name=', cfg.get('name'), 'tags=', cfg.get('tags'))

    return 0 if ok else 2


if __name__ == '__main__':
    raise SystemExit(main())
