#!/usr/bin/env python3
"""Run the "sysprep existing VM" workflow synchronously against a real VM.

This drives the exact Celery task body (sysprep_existing_vm_task) WITHOUT needing
Redis, a Celery worker, or the web app. It runs the task eagerly in a background
thread and streams progress from the task's DB row so you get live feedback
during the (multi-minute) generalize/reboot cycle.

WARNING: this GENERALIZES and REBOOTS the target VM. Only run it against a
throwaway/disposable VM.

Example:
    venv/bin/python scripts/sysprep_test.py \
        --vmid 145 \
        --hostname WINSRV19-01 \
        --ip 192.168.100.50 --netmask 24 \
        --gateway 192.168.100.1 \
        --dns 192.168.100.1,1.1.1.1 \
        --admin-password 'Sup3r$ecret!'

Requires a populated .env (PROXMOX_HOST/USER/PASSWORD, SECRET_KEY).
"""
import argparse
import os
import sys
import threading
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))

from app import app, db  # noqa: E402
from app.models import Task  # noqa: E402
from app.proxmox import get_primary_mac_address, _get_vm_node  # noqa: E402


def _parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--vmid', required=True, help='Target VM id (disposable!).')
    p.add_argument('--hostname', required=True, help='New Windows hostname (<=15 chars).')
    p.add_argument('--network-mode', choices=['static', 'dhcp'], default='static',
                   help='Network mode (default: static).')
    p.add_argument('--ip', help='Static IPv4 address (required for --network-mode static).')
    p.add_argument('--netmask', help='Prefix length (CIDR), e.g. 24 (static only).')
    p.add_argument('--gateway', help='Default gateway IPv4 (static only).')
    p.add_argument('--dns', default='', help='Comma-separated DNS servers (optional under DHCP).')
    p.add_argument('--admin-password', required=True, help='Local Administrator password to set.')
    p.add_argument('--timezone', default='Central European Standard Time',
                   help='Windows time zone id.')
    # Domain join (optional).
    p.add_argument('--join-domain', action='store_true', help='Join the machine to a domain.')
    p.add_argument('--domain', help='AD domain name, e.g. corp.example.com.')
    p.add_argument('--domain-user', help='Domain join account username.')
    p.add_argument('--domain-password', help='Domain join account password.')
    p.add_argument('--domain-ou', default='', help='Target OU DN (optional).')
    p.add_argument('--yes', action='store_true', help='Skip the confirmation prompt.')
    args = p.parse_args()

    if args.network_mode == 'static' and not all([args.ip, args.netmask, args.gateway]):
        p.error('--ip, --netmask and --gateway are required for --network-mode static')
    if args.join_domain and not all([args.domain, args.domain_user, args.domain_password]):
        p.error('--domain, --domain-user and --domain-password are required with --join-domain')
    return args


def _preflight(vmid):
    """Confirm the VM exists and report its node + primary MAC before we start."""
    node = _get_vm_node(vmid)
    if not node:
        print(f"ERROR: VM {vmid} not found (or Proxmox unreachable). Check .env.")
        return False
    mac = get_primary_mac_address(vmid, node=node)
    print(f"Preflight: VM {vmid} on node '{node}', primary MAC = {mac or 'unknown'}")
    return True


def _run_task(task_id, data):
    """Execute the task body eagerly. Exceptions are caught inside the task and
    recorded on the DB row, so this rarely raises."""
    # Imported here so the app is fully initialised first.
    from app.celery_app import sysprep_existing_vm_task
    sysprep_existing_vm_task.apply(args=[task_id, data])


def _poll_until_done(task_id, poll_interval=5):
    """Print progress transitions until the task reaches a terminal state."""
    last = None
    terminal = {'SUCCESS', 'FAILURE'}
    while True:
        status = progress = message = None
        for _ in range(3):  # small retry in case SQLite is briefly locked
            try:
                with app.app_context():
                    t = db.session.get(Task, task_id)
                    if t:
                        status, progress, message = t.status, t.progress, t.message
                break
            except Exception:  # noqa: BLE001
                time.sleep(1)
        snapshot = (status, progress, message)
        if snapshot != last:
            print(f"  [{status or 'PENDING':8}] {progress or 0:3}%  {message or ''}")
            last = snapshot
        if status in terminal:
            return status
        time.sleep(poll_interval)


def main():
    args = _parse_args()
    data = {
        'vmid': args.vmid,
        'hostname': args.hostname,
        'network_mode': args.network_mode,
        'ip_address': args.ip,
        'netmask_cidr': args.netmask,
        'gateway': args.gateway,
        'dns_servers': args.dns,
        'administrator_password': args.admin_password,
        'timezone': args.timezone,
        'join_domain': args.join_domain,
        'domain_name': args.domain,
        'domain_username': args.domain_user,
        'domain_password': args.domain_password,
        'domain_ou': args.domain_ou,
    }

    if args.network_mode == 'dhcp':
        net = f"DHCP (dns {args.dns or 'auto'})"
    else:
        net = f"{args.ip}/{args.netmask} gw {args.gateway} dns {args.dns}"
    domain = f"join {args.domain} (OU={args.domain_ou or 'default'})" if args.join_domain else "no"

    print("Sysprep existing-VM test (synchronous, no Celery/Redis/web)")
    print(f"  Target VM : {args.vmid}")
    print(f"  Hostname  : {args.hostname}")
    print(f"  Network   : {net}")
    print(f"  Domain    : {domain}")
    print("  NOTE: this GENERALIZES and REBOOTS the VM.\n")

    with app.app_context():
        db.create_all()
        if not _preflight(args.vmid):
            return 2

    if not args.yes:
        resp = input("Proceed? Type 'yes' to continue: ").strip().lower()
        if resp != 'yes':
            print("Aborted.")
            return 1

    import uuid
    task_id = str(uuid.uuid4())
    with app.app_context():
        t = Task(id=task_id, name='Sysprep Test',
                 description=f'Sysprep test for VM {args.vmid} ({args.hostname})')
        db.session.add(t)
        db.session.commit()

    worker = threading.Thread(target=_run_task, args=(task_id, data), daemon=True)
    worker.start()

    print("Progress:")
    status = _poll_until_done(task_id)
    worker.join(timeout=10)

    print()
    if status == 'SUCCESS':
        print("Result: SUCCESS")
        return 0
    print("Result: FAILURE (see message above and the worker log)")
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
