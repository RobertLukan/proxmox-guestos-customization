#!/usr/bin/env python3
"""Lab full-feature sysprep smoke: domain join + disks + DNS.

Exercises nearly the full Clone+Sysprep surface that the minimal DHCP smoke
skips. Destructive: clones a Server template, Syspreps, joins AD, attaches
pagefile/data disks, polls to SUCCESS/FAILURE.

Defaults match the GuestOS lab AD:

  domain      lab.test
  DNS         192.168.123.191
  join user   administrator@lab.test
  join pass   ChangeMe123!
  template    130 (override with --template-vmid)

Example (on the GuestOS host, or anywhere that can reach it):

  export GUESTOS_API_TOKEN=...
  python3 scripts/lab_full_feature_smoke.py \\
    --base-url http://127.0.0.1:5001 \\
    --template-vmid 130 --poll [--cleanup]

For Windows Server, manage_disks is on by default. Pass --no-disks for Win11
(template 127). Needs lab.test DC reachable at the DNS IP after DHCP.
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


def _req(method, url, token=None, body=None, timeout=60, insecure=False):
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


def _unique_hostname(prefix: str = 'S22F') -> str:
    """Windows NetBIOS-safe name (<=15 chars)."""
    prefix = (prefix or 'S22F').strip().upper()[:5] or 'S22F'
    suffix = str(int(time.time()))[-8:]
    return f'{prefix}{suffix}'[:15]


def _cleanup_result_vmid(vmid: int, remote_id: str = '') -> None:
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    env_path = os.path.join(repo_root, '.env')
    if os.path.isfile(env_path):
        try:
            from dotenv import load_dotenv

            load_dotenv(env_path)
        except ImportError:
            pass

    from app import app
    from app.proxmox import delete_vm, use_pve_override
    from app.remotes import attach_pve_override, resolve_pve_remote

    data = {}
    if remote_id:
        data['remote_id'] = remote_id
    with app.app_context():
        if remote_id:
            from app.routes import _json_field_error

            ok, err = resolve_pve_remote(data, _json_field_error)
            if not ok:
                raise RuntimeError(f'cleanup remote_id resolve failed: {err}')
        with use_pve_override(attach_pve_override(data)):
            delete_vm(vmid)
    print(f'OK   cleaned up result VMID {vmid}')


def _default_disk_plan(pagefile_gb: int, data_gb: int, data2_gb: int):
    disks = [
        {'role': 'os'},
        {
            'role': 'pagefile',
            'size_gb': pagefile_gb,
            'drive_letter': 'P',
            'ensure_pagefile': True,
            'label': 'Pagefile',
        },
        {
            'role': 'data',
            'size_gb': data_gb,
            'drive_letter': 'D',
            'label': 'Data',
        },
    ]
    if data2_gb > 0:
        disks.append(
            {
                'role': 'data',
                'size_gb': data2_gb,
                'drive_letter': 'E',
                'label': 'Apps',
            }
        )
    return disks


def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--base-url', default=os.environ.get('GUESTOS_URL', 'http://127.0.0.1:5001'))
    p.add_argument('--token', default=os.environ.get('GUESTOS_API_TOKEN', ''))
    p.add_argument('--insecure', action='store_true')
    p.add_argument('--template-vmid', type=int, default=130)
    p.add_argument('--hostname', default='')
    p.add_argument('--remote-id', default='')
    p.add_argument('--bridge', default=os.environ.get('PRIMARY_BRIDGE', 'vmbr0'))
    p.add_argument('--network-mode', default='dhcp', choices=['dhcp', 'static'])
    p.add_argument('--ip-address', default='')
    p.add_argument('--netmask-cidr', default='24')
    p.add_argument('--gateway', default='')
    p.add_argument(
        '--dns-servers',
        default='192.168.123.191',
        help='DNS for domain join (default: lab DC).',
    )
    p.add_argument('--domain-name', default='lab.test')
    p.add_argument('--domain-username', default='administrator@lab.test')
    p.add_argument('--domain-password', default='ChangeMe123!')
    p.add_argument('--domain-ou', default='', help='Optional OU path for Add-Computer.')
    p.add_argument('--admin-password', default='ChangeMe123!')
    p.add_argument('--cores', type=int, default=4)
    p.add_argument('--ram', type=int, default=8192)
    p.add_argument('--pagefile-gb', type=int, default=8)
    p.add_argument('--data-gb', type=int, default=20)
    p.add_argument(
        '--data2-gb',
        type=int,
        default=10,
        help='Second data disk size GB (0 to skip). Default 10 → drive E:.',
    )
    p.add_argument(
        '--no-disks',
        action='store_true',
        help='Omit manage_disks (required for Win11 / non-Server templates).',
    )
    p.add_argument('--fast-waits', action='store_true', default=True)
    p.add_argument('--no-fast-waits', action='store_false', dest='fast_waits')
    p.add_argument('--poll', action='store_true', default=True)
    p.add_argument('--no-poll', action='store_false', dest='poll')
    p.add_argument('--poll-seconds', type=int, default=3600)
    p.add_argument('--cleanup', action='store_true')
    p.add_argument('--cleanup-on-failure', action='store_true')
    args = p.parse_args()

    if not args.token:
        print('GUESTOS_API_TOKEN / --token required', file=sys.stderr)
        return 1

    base = args.base_url.rstrip('/')
    insecure = args.insecure

    code, health = _req('GET', f'{base}/api/health', insecure=insecure)
    if code != 200 or health.get('status') != 'ok':
        print(f'FAIL health: HTTP {code} {health}', file=sys.stderr)
        return 1
    print(f'OK   health: {health}')

    code, ver = _req('GET', f'{base}/api/version', insecure=insecure)
    if code != 200:
        print(f'FAIL version: HTTP {code} {ver}', file=sys.stderr)
        return 1
    print(f'OK   version: {ver}')

    hostname = args.hostname.strip() or _unique_hostname()
    disks = (
        []
        if args.no_disks
        else _default_disk_plan(args.pagefile_gb, args.data_gb, args.data2_gb)
    )

    payload = {
        'template_vmid': args.template_vmid,
        'hostname': hostname,
        'cores': args.cores,
        'ram': args.ram,
        'bridge': args.bridge,
        'network_mode': args.network_mode,
        'dns_servers': args.dns_servers,
        'administrator_password': args.admin_password,
        'timezone': 'Central European Standard Time',
        'locale': 'en-US',
        # Inline AD credentials (not DOMAIN_PROFILES).
        'join_domain': True,
        'use_domain_profile_credentials': False,
        'domain_name': args.domain_name,
        'domain_username': args.domain_username,
        'domain_password': args.domain_password,
        'manage_disks': not args.no_disks,
        'fast_waits': bool(args.fast_waits),
    }
    if not args.no_disks:
        payload['disks'] = disks
    if args.domain_ou.strip():
        payload['domain_ou'] = args.domain_ou.strip()
    if args.network_mode == 'static':
        payload['ip_address'] = args.ip_address
        payload['netmask_cidr'] = args.netmask_cidr
        payload['gateway'] = args.gateway
    if args.remote_id:
        payload['remote_id'] = args.remote_id

    disk_roles = [d['role'] for d in disks] if disks else ['(none)']
    disk_sizes = (
        [d.get('size_gb') or d.get('grow_to_gb') or '-' for d in disks]
        if disks
        else ['-']
    )
    print(
        '     full-feature payload: '
        f"template={payload['template_vmid']} host={payload['hostname']} "
        f"net={payload['network_mode']} dns={payload['dns_servers']} "
        f"domain={payload['domain_name']} user={payload['domain_username']} "
        f"manage_disks={payload['manage_disks']} "
        f"disks={disk_roles} sizes={disk_sizes}"
    )

    # Never log the domain password.
    safe = {k: v for k, v in payload.items() if k != 'domain_password'}
    safe['domain_password'] = '***'
    print(f'     json: {json.dumps(safe, separators=(",", ":"))}')

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
    if body.get('warnings'):
        print(f'WARN start warnings: {body["warnings"]}')
    print(f'OK   started task_id={task_id}')

    if not args.poll:
        print('Done (re-run with --poll to wait).')
        return 0

    deadline = time.time() + args.poll_seconds
    final = None
    while time.time() < deadline:
        code, st = _req(
            'GET',
            f'{base}/task_status/{task_id}',
            token=args.token,
            insecure=insecure,
        )
        if code != 200:
            print(f'FAIL poll: HTTP {code} {st}', file=sys.stderr)
            return 1
        print(
            f"     [{st.get('progress')}%] {st.get('status')} "
            f"vmid={st.get('result_vmid')} {st.get('message')}"
        )
        if st.get('status') in ('SUCCESS', 'FAILURE', 'CANCELLED'):
            final = st
            break
        time.sleep(10)
    else:
        print(f'FAIL poll timeout after {args.poll_seconds}s', file=sys.stderr)
        return 2

    result_vmid = final.get('result_vmid')
    status = final.get('status')
    print(f'OK   final status={status} result_vmid={result_vmid} msg={final.get("message")}')

    do_cleanup = (status == 'SUCCESS' and args.cleanup) or (
        status != 'SUCCESS' and args.cleanup_on_failure and result_vmid
    )
    if do_cleanup and result_vmid:
        try:
            _cleanup_result_vmid(int(result_vmid), remote_id=args.remote_id)
        except Exception as e:
            print(f'FAIL cleanup VMID {result_vmid}: {e}', file=sys.stderr)
            return 3

    return 0 if status == 'SUCCESS' else 2


if __name__ == '__main__':
    # Line-buffer stdout when redirected (nohup / docker logs).
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass
    sys.exit(main())
