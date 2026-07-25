#!/usr/bin/env python3
"""Smoke-check GuestOS machine API from a PDM host (or any client).

Default: GET /api/health and /api/version, then verify the API token is
accepted (401 without token on a protected route). Does not start sysprep
unless --start-existing is passed.

Examples:
  python3 scripts/pdm_api_smoke.py --base-url http://127.0.0.1:5001 --token "$GUESTOS_API_TOKEN"
  python3 scripts/pdm_api_smoke.py --base-url http://guestos:5001 --token "$GUESTOS_API_TOKEN" \\
      --start-existing --vmid 121 --hostname LABTEST01 --remote-id lab
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request


def _req(method, url, token=None, body=None, timeout=30):
    data = None
    headers = {'Accept': 'application/json'}
    if body is not None:
        data = json.dumps(body).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    if token:
        headers['Authorization'] = f'Bearer {token}'
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read().decode('utf-8')
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8', errors='replace')
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {'error': raw}
        return e.code, payload


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--base-url', default=os.environ.get('GUESTOS_URL', 'http://127.0.0.1:5001'))
    p.add_argument('--token', default=os.environ.get('GUESTOS_API_TOKEN', ''))
    p.add_argument('--start-existing', action='store_true', help='POST start_sysprep_existing_vm_task (destructive).')
    p.add_argument('--vmid', type=int)
    p.add_argument('--hostname', default='LABTEST01')
    p.add_argument('--remote-id', default='')
    p.add_argument('--admin-password', default='ChangeMe123!')
    p.add_argument('--poll', action='store_true', help='Poll task_status after --start-existing.')
    p.add_argument(
        '--poll-seconds',
        type=int,
        default=1800,
        help='Max seconds to poll (default 1800). Sysprep often needs 10–30+ minutes.',
    )
    args = p.parse_args()
    base = args.base_url.rstrip('/')

    print(f'Base URL: {base}')
    code, health = _req('GET', f'{base}/api/health')
    if code != 200 or health.get('status') != 'ok':
        print(f'FAIL health: HTTP {code} {health}', file=sys.stderr)
        return 1
    print(f'OK   health: {health}')

    code, ver = _req('GET', f'{base}/api/version')
    if code != 200 or not ver.get('version'):
        print(f'FAIL version: HTTP {code} {ver}', file=sys.stderr)
        return 1
    print(f'OK   version: {ver["version"]}')

    code, _ = _req('POST', f'{base}/start_sysprep_existing_vm_task', body={'hostname': 'X'})
    if code != 401:
        print(f'FAIL expected 401 without token, got {code}', file=sys.stderr)
        return 1
    print('OK   unauthorized without token')

    if not args.token:
        print('SKIP authenticated checks (set --token or GUESTOS_API_TOKEN)')
        return 0

    if not args.start_existing:
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
        )
        if code != 400 or 'domain_profile' not in (body.get('errors') or {}):
            print(f'FAIL token auth smoke: HTTP {code} {body}', file=sys.stderr)
            return 1
        print('OK   token accepted (got expected domain_profile 400)')
        print('Done (pass --start-existing to enqueue a real job).')
        return 0

    if not args.vmid:
        print('--start-existing requires --vmid', file=sys.stderr)
        return 1

    payload = {
        'vmid': args.vmid,
        'hostname': args.hostname,
        'network_mode': 'dhcp',
        'administrator_password': args.admin_password,
        'timezone': 'Central European Standard Time',
        'join_domain': False,
    }
    if args.remote_id:
        payload['remote_id'] = args.remote_id

    print(
        f"     payload: vmid={payload['vmid']} hostname={payload['hostname']} "
        f"network_mode=dhcp admin_password={'set' if payload['administrator_password'] else 'MISSING'}"
    )
    code, body = _req('POST', f'{base}/start_sysprep_existing_vm_task', token=args.token, body=payload)
    if code != 200 or not body.get('task_id'):
        print(f'FAIL start: HTTP {code} {body}', file=sys.stderr)
        return 1
    task_id = body['task_id']
    print(f'OK   started task_id={task_id}')

    if args.poll:
        deadline = time.time() + args.poll_seconds
        while time.time() < deadline:
            code, st = _req('GET', f'{base}/task_status/{task_id}', token=args.token)
            if code != 200:
                print(f'FAIL poll: HTTP {code} {st}', file=sys.stderr)
                return 1
            print(f"     status={st.get('status')} progress={st.get('progress')} msg={st.get('message')}")
            if st.get('status') in ('SUCCESS', 'FAILURE'):
                return 0 if st.get('status') == 'SUCCESS' else 2
            time.sleep(5)
        print(
            f'FAIL poll timeout after {args.poll_seconds}s '
            f'(task may still be running — check UI workflow or: '
            f'curl -H "Authorization: Bearer $GUESTOS_API_TOKEN" '
            f'"{base}/task_status/{task_id}")',
            file=sys.stderr,
        )
        return 2

    return 0


if __name__ == '__main__':
    sys.exit(main())
