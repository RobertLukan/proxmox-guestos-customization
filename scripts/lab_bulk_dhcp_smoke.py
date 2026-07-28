#!/usr/bin/env python3
"""Lab: 2x Win11 bulk DHCP using a Customization Spec (identity fields only)."""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get('GUESTOS_URL', 'http://127.0.0.1:5001').rstrip('/')
TOKEN = os.environ.get('GUESTOS_API_TOKEN', '')


def req(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    headers = {'Authorization': f'Bearer {TOKEN}', 'Accept': 'application/json'}
    if body is not None:
        headers['Content-Type'] = 'application/json'
    request = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as resp:
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
    if not TOKEN:
        print('GUESTOS_API_TOKEN required', file=sys.stderr)
        return 1

    spec_name = f'BulkDHCP-Lab-{int(time.time()) % 100000}'
    code, spec = req(
        'POST',
        '/api/specs',
        {
            'name': spec_name,
            'description': 'Lab bulk DHCP smoke — identity only',
            'payload': {
                'timezone': 'Central European Standard Time',
                'locale': 'en-US',
                'workgroup': 'GUESTOSLAB',
                'join_domain': False,
            },
        },
    )
    print('create_spec', code, {
        'id': spec.get('id'),
        'name': spec.get('name'),
        'payload': spec.get('payload') or {},
    })
    if code not in (200, 201):
        print('spec create failed', spec, file=sys.stderr)
        return 1
    spec_id = spec['id']

    suffix = str(int(time.time()))[-6:]
    hosts = [f'VDI{suffix}A', f'VDI{suffix}B']
    body = {
        'request_id': f'bulk-dhcp-{suffix}',
        'shared': {
            'template_vmid': 127,
            'cores': 2,
            'ram': 4096,
            'bridge': 'vmbr0',
            'network_mode': 'dhcp',
            'administrator_password': 'ChangeMe123!',
            'join_domain': False,
            'fast_waits': True,
            'spec_id': spec_id,
        },
        'items': [{'hostname': h} for h in hosts],
    }
    print('start_bulk hosts=', hosts, 'spec_id=', spec_id)
    code, batch = req('POST', '/start_sysprep_bulk_workflow', body)
    print(
        'start_bulk',
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
            )
        },
    )
    if code != 200 or not batch.get('batch_id'):
        return 2
    batch_id = batch['batch_id']

    deadline = time.time() + 2400
    final_tasks = None
    while time.time() < deadline:
        code, st = req('GET', f'/api/batches/{batch_id}')
        if code != 200:
            print('batch_poll_fail', code, st)
            time.sleep(5)
            continue
        summary = {
            k: st.get(k)
            for k in (
                'status',
                'accepted_items',
                'succeeded_items',
                'failed_items',
                'running_items',
                'message',
            )
        }
        tasks = st.get('tasks') or []
        if not tasks:
            code2, tl = req('GET', f'/api/tasks?batch_id={batch_id}')
            tasks = (tl.get('tasks') if code2 == 200 else []) or []
        short = []
        for t in tasks:
            msg = (t.get('message') or '')[:70]
            short.append(f"{t.get('hostname')}:{t.get('status')}:{t.get('progress')}:{msg}")
        print('batch', summary, '|', ' || '.join(short))
        statuses = [t.get('status') for t in tasks]
        if statuses and all(s in ('SUCCESS', 'FAILURE', 'CANCELLED') for s in statuses):
            final_tasks = tasks
            break
        time.sleep(10)
    else:
        print('TIMEOUT waiting for bulk batch', file=sys.stderr)
        return 3

    ok = len(final_tasks) == len(hosts) and all(t.get('status') == 'SUCCESS' for t in final_tasks)
    print('RESULT', 'PASS' if ok else 'FAIL')
    for t in final_tasks:
        print('---', t.get('hostname'), t.get('status'), 'vmid=', t.get('result_vmid'))
        print((t.get('message') or '')[:240])

    # Inspect lifecycle tags on result VMs.
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    from app import create_app
    from app.proxmox import _get_vm_node, get_proxmox_api

    app = create_app()
    with app.app_context():
        p = get_proxmox_api()
        for t in final_tasks:
            vmid = t.get('result_vmid')
            if not vmid:
                continue
            node = _get_vm_node(int(vmid))
            cfg = p.nodes(node).qemu(int(vmid)).config.get()
            print('pve', vmid, 'name=', cfg.get('name'), 'tags=', cfg.get('tags'))

    return 0 if ok else 2


if __name__ == '__main__':
    raise SystemExit(main())
