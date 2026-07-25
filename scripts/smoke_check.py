#!/usr/bin/env python3
"""Tier-0 read-only smoke check against a live Proxmox lab.

This script performs ONLY non-mutating API calls. It never clones, powers on,
reconfigures, or deletes anything. It reuses the application's own helpers so it
exercises the exact code paths the web app relies on.

Usage:
    python scripts/smoke_check.py            # run all read-only checks
    python scripts/smoke_check.py --vmid 123 # also resolve a WinRM IP for VM 123

Requires a populated .env (at minimum: SECRET_KEY, PROXMOX_HOST, PROXMOX_USER,
PROXMOX_PASSWORD). Optional: WINRM_SUBNET, PRIMARY_BRIDGE, TEMP_BRIDGE.
"""
import argparse
import os
import sys

# Make sure the repo root is importable when run as `python scripts/smoke_check.py`.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))

# Importing the app builds the Flask app + config (fails fast if SECRET_KEY is
# missing, matching real startup behaviour).
from app import app  # noqa: E402
from app import proxmox as pm  # noqa: E402


# --- tiny reporting helpers -------------------------------------------------

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
WARN = "\033[33mWARN\033[0m"
INFO = "\033[36mINFO\033[0m"

_failures = 0


def _mark_fail():
    global _failures
    _failures += 1


def line(status, msg):
    print(f"  [{status}] {msg}")


def header(title):
    print(f"\n=== {title} ===")


# --- checks -----------------------------------------------------------------

def check_config():
    header("Configuration")
    host = app.config.get("PROXMOX_HOST")
    user = app.config.get("PROXMOX_USER")
    pw = app.config.get("PROXMOX_PASSWORD")
    line(INFO, f"PROXMOX_HOST      = {host or '(unset)'}")
    line(INFO, f"PROXMOX_USER      = {user or '(unset)'}")
    line(INFO, f"PROXMOX_PASSWORD  = {'set' if pw else '(unset)'}")
    line(INFO, f"PROXMOX_VERIFY_SSL= {app.config.get('PROXMOX_VERIFY_SSL')}")
    line(INFO, f"WINRM_SUBNET      = {app.config.get('WINRM_SUBNET') or '(unset)'}")
    line(INFO, f"PRIMARY_BRIDGE    = {app.config.get('PRIMARY_BRIDGE')}")
    line(INFO, f"TEMP_BRIDGE       = {app.config.get('TEMP_BRIDGE')}")
    ok = all([host, user, pw])
    if not ok:
        line(FAIL, "Missing one of PROXMOX_HOST/USER/PASSWORD in .env")
        _mark_fail()
    return ok


def check_connection():
    header("Connection")
    proxmox = pm.get_proxmox_api()
    if not proxmox:
        line(FAIL, "get_proxmox_api() returned None (see log above)")
        _mark_fail()
        return None
    try:
        version = proxmox.version.get()
        line(PASS, f"Connected. PVE version: {version.get('version')} "
                   f"(release {version.get('release')})")
    except Exception as e:  # noqa: BLE001
        line(FAIL, f"Connected object created but version query failed: {e}")
        _mark_fail()
        return None
    return proxmox


def check_nodes(proxmox):
    header("Nodes")
    try:
        nodes = proxmox.nodes.get()
    except Exception as e:  # noqa: BLE001
        line(FAIL, f"Could not list nodes: {e}")
        _mark_fail()
        return
    if not nodes:
        line(WARN, "No nodes returned.")
        return
    for n in nodes:
        line(PASS, f"{n.get('node')}: status={n.get('status')} "
                   f"cpu={round((n.get('cpu') or 0) * 100, 1)}% "
                   f"mem={_gib(n.get('mem'))}/{_gib(n.get('maxmem'))} GiB")


def check_templates():
    header("Templates")
    templates = pm.get_template_vms()
    if not templates:
        line(WARN, "No templates found (get_template_vms() empty).")
        return
    for t in templates:
        line(PASS, f"vmid={t.get('vmid')} name={t.get('name')} node={t.get('node')}")


def check_bridges():
    header("Network bridges")
    bridges = pm.get_network_bridges()
    if not bridges:
        line(WARN, "No bridges found (get_network_bridges() empty).")
        return
    names = {b.get("iface") for b in bridges}
    for b in bridges:
        line(PASS, f"{b.get('iface')} on node-level (type={b.get('type')})")
    for cfg_key in ("PRIMARY_BRIDGE", "TEMP_BRIDGE"):
        want = app.config.get(cfg_key)
        if want in names:
            line(PASS, f"{cfg_key}={want} exists")
        else:
            line(WARN, f"{cfg_key}={want} not found among discovered bridges")


def check_manageable_vms():
    header("Manageable VMs (running + lifecycle- tag)")
    vms = pm.get_manageable_vms()
    if not vms:
        line(INFO, "No manageable VMs (this is normal on a clean lab).")
        return
    for vm in vms:
        line(PASS, f"vmid={vm['vmid']} name={vm['name']} node={vm['node']} "
                   f"tags={vm['tags']}")


def check_winrm_ip(vmid):
    header(f"WinRM IP resolution for VM {vmid}")
    ip = pm.select_winrm_ip(vmid)
    if ip:
        line(PASS, f"select_winrm_ip({vmid}) -> {ip}")
    else:
        line(WARN, f"select_winrm_ip({vmid}) -> None "
                   "(guest agent not ready, or no IP in WINRM_SUBNET)")


def _gib(n):
    try:
        return round(int(n) / (1024 ** 3), 1)
    except (TypeError, ValueError):
        return "?"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vmid", type=int, default=None,
                        help="Optionally resolve a WinRM IP for this VM (read-only).")
    args = parser.parse_args()

    print("Proxmox GuestOS Customization - Tier-0 read-only smoke check")
    print("(no VMs are created, modified, or deleted)")

    with app.app_context():
        if not check_config():
            print(f"\nResult: {FAIL} - fix configuration and retry.")
            return 2
        proxmox = check_connection()
        if not proxmox:
            print(f"\nResult: {FAIL} - could not reach Proxmox API.")
            return 2
        check_nodes(proxmox)
        check_templates()
        check_bridges()
        check_manageable_vms()
        if args.vmid is not None:
            check_winrm_ip(args.vmid)

    print()
    if _failures:
        print(f"Result: {FAIL} - {_failures} critical check(s) failed.")
        return 1
    print(f"Result: {PASS} - all critical read-only checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
