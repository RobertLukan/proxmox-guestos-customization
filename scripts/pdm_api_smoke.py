#!/usr/bin/env python3
"""Smoke-check GuestOS machine API (CI-safe by default; lab live optional).

Default: GET /api/health and /api/version, then verify the API token is
accepted. Does not start sysprep unless --start-workflow is passed.

Lab live example (destructive — clones a template, polls, deletes clone). Run after major changes, not on a daily schedule:

  python3 scripts/pdm_api_smoke.py \\
    --base-url https://192.168.123.197 --insecure \\
    --token "$GUESTOS_API_TOKEN" \\
    --start-workflow --template-vmid 120 --remote-id vie-1 \\
    --poll --cleanup

Cleanup uses the app's Proxmox helpers (needs repo + .env on the GuestOS host,
or run via ``docker exec`` into the web container).
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


def _req(method, url, token=None, body=None, timeout=30, insecure=False):
    data = None
    headers = {'Accept': 'application/json'}
    if body is not None:
        data = json.dumps(body).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    if token:
        headers['Authorization'] = f'Bearer {token}'
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(
            request, timeout=timeout, context=_ssl_context(insecure)
        ) as resp:
            raw = resp.read().decode('utf-8')
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8', errors='replace')
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {'error': raw}
        return e.code, payload


def _unique_hostname(prefix: str = 'SMOKE') -> str:
    """Windows NetBIOS-safe name (<=15 chars): PREFIX + epoch suffix."""
    prefix = (prefix or 'SMOKE').strip().upper()[:5] or 'SMOKE'
    suffix = str(int(time.time()))[-8:]
    return f'{prefix}{suffix}'[:15]


def _cleanup_result_vmid(vmid: int, remote_id: str = '') -> None:
    """Delete the clone via GuestOS Proxmox helpers (run on GuestOS host)."""
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    # Load .env the same way the app does when present.
    env_path = os.path.join(repo_root, '.env')
    if os.path.isfile(env_path):
        try:
            from dotenv import load_dotenv

            load_dotenv(env_path)
        except ImportError:
            pass

    from app import app
    from app.proxmox import delete_vm, use_pve_override
    from app.remotes import resolve_pve_remote

    data = {}
    if remote_id:
        data['remote_id'] = remote_id
    with app.app_context():
        if remote_id:
            from app.routes import _json_field_error

            ok, err = resolve_pve_remote(data, _json_field_error)
            if not ok:
                raise RuntimeError(f'cleanup remote_id resolve failed: {err}')
        with use_pve_override(data.get('_pve')):
            delete_vm(vmid)
    print(f'OK   cleaned up result VMID {vmid}')


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--base-url', default=os.environ.get('GUESTOS_URL', 'http://127.0.0.1:5001'))
    p.add_argument('--token', default=os.environ.get('GUESTOS_API_TOKEN', ''))
    p.add_argument(
        '--insecure',
        action='store_true',
        help='Skip TLS certificate verification (lab self-signed Caddy).',
    )
    p.add_argument(
        '--start-workflow',
        action='store_true',
        help='POST start_sysprep_workflow (destructive: clone template + Sysprep).',
    )
    p.add_argument('--template-vmid', type=int, help='Template VMID for --start-workflow.')
    p.add_argument(
        '--hostname',
        default='',
        help='Guest hostname (default: unique SMOKE######## when starting a workflow).',
    )
    p.add_argument('--remote-id', default='')
    p.add_argument(
        '--bridge',
        default=os.environ.get('PRIMARY_BRIDGE', 'vmbr0'),
        help='Proxmox bridge for the clone NIC (default: PRIMARY_BRIDGE or vmbr0).',
    )
    p.add_argument('--network-mode', default='dhcp', choices=['dhcp', 'static'])
    p.add_argument('--ip-address', default='')
    p.add_argument('--netmask-cidr', default='24')
    p.add_argument('--gateway', default='')
    p.add_argument('--dns-servers', default='')
    p.add_argument(
        '--manage-disks',
        action='store_true',
        help='Enable disk reconcile (default plan: os + 16G pagefile + 50G data).',
    )
    p.add_argument(
        '--fast-waits',
        action='store_true',
        default=True,
        help='Shorten first-boot settle + agent stability waits (default on for smoke).',
    )
    p.add_argument(
        '--no-fast-waits',
        action='store_false',
        dest='fast_waits',
        help='Use production wait timings (3 min settle, 60s agent stable).',
    )
    p.add_argument('--admin-password', default='ChangeMe123!')
    p.add_argument('--poll', action='store_true', help='Poll task_status after --start-workflow.')
    p.add_argument(
        '--poll-seconds',
        type=int,
        default=1800,
        help='Max seconds to poll (default 1800). Sysprep often needs 10–30+ minutes.',
    )
    p.add_argument(
        '--cleanup',
        action='store_true',
        help='After a successful poll, delete the result VM via Proxmox (needs .env on this host).',
    )
    p.add_argument(
        '--cleanup-on-failure',
        action='store_true',
        help='Also delete result_vmid if the task ends in FAILURE (when a clone was created).',
    )
    args = p.parse_args()
    base = args.base_url.rstrip('/')
    insecure = args.insecure

    print(f'Base URL: {base}')
    code, health = _req('GET', f'{base}/api/health', insecure=insecure)
    if code != 200 or health.get('status') != 'ok':
        print(f'FAIL health: HTTP {code} {health}', file=sys.stderr)
        return 1
    print(f'OK   health: {health}')

    code, ver = _req('GET', f'{base}/api/version', insecure=insecure)
    if code != 200 or not ver.get('version'):
        print(f'FAIL version: HTTP {code} {ver}', file=sys.stderr)
        return 1
    print(f'OK   version: {ver["version"]}')

    code, _ = _req('POST', f'{base}/start_sysprep_workflow', body={'hostname': 'X'}, insecure=insecure)
    if code != 401:
        print(f'FAIL expected 401 without token, got {code}', file=sys.stderr)
        return 1
    print('OK   unauthorized without token')

    if not args.token:
        print('SKIP authenticated checks (set --token or GUESTOS_API_TOKEN)')
        return 0

    # Existing-VM Sysprep must stay disabled (protect production).
    code, body = _req(
        'POST',
        f'{base}/start_sysprep_existing_vm_task',
        token=args.token,
        body={'vmid': 1, 'hostname': 'X', 'join_domain': False},
        insecure=insecure,
    )
    if code != 403:
        print(f'FAIL expected 403 for existing-VM sysprep, got {code} {body}', file=sys.stderr)
        return 1
    print('OK   existing-VM sysprep disabled (403)')

    if not args.start_workflow:
        # Lightweight auth check: bad profile still proves token + CSRF skip.
        code, body = _req(
            'POST',
            f'{base}/start_sysprep_workflow',
            token=args.token,
            body={
                'hostname': 'TOKCHECK',
                'join_domain': True,
                'use_domain_profile_credentials': True,
                'domain_profile': '__smoke_missing_profile__',
            },
            insecure=insecure,
        )
        if code != 400 or 'domain_profile' not in (body.get('errors') or {}):
            print(f'FAIL token auth smoke: HTTP {code} {body}', file=sys.stderr)
            return 1
        print('OK   token accepted (got expected domain_profile 400)')
        print('Done (pass --start-workflow --template-vmid N --poll [--cleanup] for lab live).')
        return 0

    if not args.template_vmid:
        print('--start-workflow requires --template-vmid', file=sys.stderr)
        return 1

    hostname = args.hostname.strip() or _unique_hostname()
    payload = {
        'template_vmid': args.template_vmid,
        'hostname': hostname,
        'cores': 2,
        'ram': 4096,
        'bridge': args.bridge,
        'network_mode': args.network_mode,
        'administrator_password': args.admin_password,
        'timezone': 'Central European Standard Time',
        'join_domain': False,
    }
    if args.network_mode == 'static':
        payload['ip_address'] = args.ip_address
        payload['netmask_cidr'] = args.netmask_cidr
        payload['gateway'] = args.gateway
        if args.dns_servers:
            payload['dns_servers'] = args.dns_servers
    if args.manage_disks:
        payload['manage_disks'] = True
    if args.fast_waits:
        payload['fast_waits'] = True
    if args.remote_id:
        payload['remote_id'] = args.remote_id

    print(
        f"     payload: template_vmid={payload['template_vmid']} hostname={payload['hostname']} "
        f"bridge={payload['bridge']} network_mode={args.network_mode} "
        f"{'ip=' + args.ip_address + ' ' if args.network_mode == 'static' else ''}"
        f"manage_disks={bool(args.manage_disks)} fast_waits={bool(args.fast_waits)} "
        f"admin_password={'set' if payload['administrator_password'] else 'MISSING'}"
    )
    code, body = _req(
        'POST',
        f'{base}/start_sysprep_workflow',
        token=args.token,
        body=payload,
        insecure=insecure,
    )
    if code != 200 or not body.get('task_id'):
        print(f'FAIL start: HTTP {code} {body}', file=sys.stderr)
        return 1
    task_id = body['task_id']
    print(f'OK   started task_id={task_id}')

    if not args.poll:
        print('Done (pass --poll to wait for SUCCESS/FAILURE; --cleanup to delete the clone).')
        return 0

    deadline = time.time() + args.poll_seconds
    final = None
    while time.time() < deadline:
        code, st = _req('GET', f'{base}/task_status/{task_id}', token=args.token, insecure=insecure)
        if code != 200:
            print(f'FAIL poll: HTTP {code} {st}', file=sys.stderr)
            return 1
        print(f"     status={st.get('status')} progress={st.get('progress')} msg={st.get('message')}")
        if st.get('status') in ('SUCCESS', 'FAILURE'):
            final = st
            break
        time.sleep(5)
    else:
        print(
            f'FAIL poll timeout after {args.poll_seconds}s '
            f'(task may still be running — check UI workflow or: '
            f'curl -H "Authorization: Bearer $GUESTOS_API_TOKEN" '
            f'"{base}/task_status/{task_id}")',
            file=sys.stderr,
        )
        return 2

    result_vmid = final.get('result_vmid')
    status = final.get('status')
    do_cleanup = (status == 'SUCCESS' and args.cleanup) or (
        status == 'FAILURE' and args.cleanup_on_failure and result_vmid
    )
    if do_cleanup and result_vmid:
        try:
            _cleanup_result_vmid(int(result_vmid), remote_id=args.remote_id)
        except Exception as e:
            print(f'FAIL cleanup VMID {result_vmid}: {e}', file=sys.stderr)
            return 3
    elif args.cleanup and status == 'SUCCESS' and not result_vmid:
        print('WARN cleanup skipped (no result_vmid on SUCCESS)', file=sys.stderr)

    return 0 if status == 'SUCCESS' else 2


if __name__ == '__main__':
    sys.exit(main())
