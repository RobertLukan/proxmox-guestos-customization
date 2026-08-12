#!/usr/bin/env python3
"""Lab Linux cloud-init smoke: static/DHCP + optional disk resize + detach.

Exercises the Linux Clone+Cloud-Init surface. Destructive: clones a Linux
template, applies cloud-init customization, polls to SUCCESS/FAILURE.

Defaults match the GuestOS lab:

  template    137 (override with --template-vmid)
  ciuser      ubuntu

Example (on the GuestOS host, or anywhere that can reach it):

  export GUESTOS_API_TOKEN=...
  python3 scripts/lab_linux_smoke.py \\
    --base-url http://127.0.0.1:5001 \\
    --template-vmid 137 --network-mode static \\
    --ip-address 192.168.123.200 --gateway 192.168.123.1 \\
    --poll [--cleanup]

Multi-NIC example (JSON on stdin or --nics):

  python3 scripts/lab_linux_smoke.py \\
    --nics '[{"network_mode":"static","ip_address":"192.168.123.200",
              "netmask":24,"gateway":"192.168.123.1"},
             {"bridge":"vmbr1","network_mode":"dhcp"}]' \\
    --poll
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import ssl
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


def _unique_hostname(prefix: str = 'LNX') -> str:
    prefix = (prefix or 'LNX').strip()[:8] or 'LNX'
    suffix = str(int(time.time()))[-8:]
    return f'{prefix}-{suffix}'[:63]


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


def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--base-url', default=os.environ.get('GUESTOS_URL', 'http://127.0.0.1:5001'))
    p.add_argument('--token', default=os.environ.get('GUESTOS_API_TOKEN', ''))
    p.add_argument('--insecure', action='store_true')
    p.add_argument('--template-vmid', type=int, default=137)
    p.add_argument('--hostname', default='')
    p.add_argument('--remote-id', default='')
    p.add_argument('--bridge', default=os.environ.get('PRIMARY_BRIDGE', 'vmbr0'))
    p.add_argument('--network-mode', default='dhcp', choices=['dhcp', 'static'])
    p.add_argument('--ip-address', default='')
    # Keep CLI naming explicit, but the API expects `netmask` (integer 0-32).
    p.add_argument('--netmask-cidr', default='24')
    p.add_argument('--gateway', default='')
    p.add_argument('--dns-servers', default='', help='Comma-separated DNS servers.')
    p.add_argument('--ciuser', default='ubuntu')
    p.add_argument('--cipassword', default='')
    p.add_argument(
        '--sshkeys-file',
        default='',
        help='Path to authorized_keys file (contents sent as sshkeys).',
    )
    p.add_argument('--os-disk-gb', type=int, default=0, help='Grow OS disk to N GB (0=skip).')
    p.add_argument(
        '--detach-cloudinit',
        action='store_true',
        help='Detach cloud-init CDROM after ready (power-cycle freeze).',
    )
    p.add_argument(
        '--nics',
        default='',
        help='JSON array of NIC objects for multi-NIC testing.',
    )
    p.add_argument('--cores', type=int, default=2)
    p.add_argument('--ram', type=int, default=4096)
    p.add_argument('--fast-waits', action='store_true', default=True)
    p.add_argument('--no-fast-waits', action='store_false', dest='fast_waits')
    p.add_argument('--poll', action='store_true', default=True)
    p.add_argument('--no-poll', action='store_false', dest='poll')
    p.add_argument('--poll-seconds', type=int, default=1800)
    p.add_argument('--cleanup', action='store_true')
    p.add_argument('--cleanup-on-failure', action='store_true')
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

    from lab_smoke_preflight import ensure_lab_ram

    rc = ensure_lab_ram(
        args.ram,
        1,
        reserve_mb=args.ram_reserve_mb,
        skip=args.skip_ram_check,
    )
    if rc != 0:
        return rc

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

    payload = {
        'template_vmid': args.template_vmid,
        'hostname': hostname,
        'cores': args.cores,
        'ram': args.ram,
        'bridge': args.bridge,
        'network_mode': args.network_mode,
        'ciuser': args.ciuser,
        'fast_waits': bool(args.fast_waits),
    }

    if args.nics.strip():
        try:
            payload['nics'] = json.loads(args.nics)
        except json.JSONDecodeError as e:
            print(f'FAIL --nics JSON parse: {e}', file=sys.stderr)
            return 1
    elif args.network_mode == 'static':
        payload['ip_address'] = args.ip_address
        payload['netmask'] = int(args.netmask_cidr)
        payload['gateway'] = args.gateway

    if args.dns_servers.strip():
        payload['dns_servers'] = args.dns_servers
    if args.cipassword:
        payload['cipassword'] = args.cipassword
    if args.sshkeys_file:
        with open(args.sshkeys_file, 'r') as f:
            payload['sshkeys'] = f.read().strip()
    if args.os_disk_gb > 0:
        payload['os_disk_gb'] = args.os_disk_gb
    if args.detach_cloudinit:
        payload['detach_cloudinit_after_ready'] = True
    if args.remote_id:
        payload['remote_id'] = args.remote_id

    safe = {k: v for k, v in payload.items() if k not in ('cipassword', 'sshkeys')}
    if args.cipassword:
        safe['cipassword'] = '***'
    if args.sshkeys_file:
        safe['sshkeys'] = '(from file)'
    print(
        '     linux payload: '
        f"template={payload['template_vmid']} host={payload['hostname']} "
        f"net={payload['network_mode']} ciuser={payload.get('ciuser')} "
        f"os_disk_gb={args.os_disk_gb or '-'} "
        f"detach={args.detach_cloudinit} "
        f"nics={'custom' if args.nics.strip() else 'default'}"
    )
    print(f'     json: {json.dumps(safe, separators=(",", ":"))}')

    code, body = _req(
        'POST',
        f'{base}/start_linux_cloudinit_workflow',
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
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass
    sys.exit(main())
