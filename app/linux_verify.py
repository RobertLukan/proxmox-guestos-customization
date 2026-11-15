"""Post-customize verification for Linux cloud-init clones via QGA."""
from __future__ import annotations

import time

from app import app
from app.proxmox import get_proxmox_api, _get_vm_node, run_command_in_guest


def _guest_ipv4_ipv6(vmid):
    proxmox = get_proxmox_api()
    node = _get_vm_node(vmid)
    if not proxmox or not node:
        return [], []
    info = proxmox.nodes(node).qemu(vmid).agent.get('network-get-interfaces')
    ips = [
        addr.get('ip-address')
        for iface in info.get('result', [])
        for addr in iface.get('ip-addresses', [])
        if addr.get('ip-address-type') == 'ipv4'
        and not str(addr.get('ip-address', '')).startswith(('127.', '169.254.'))
    ]
    ips6 = [
        addr.get('ip-address')
        for iface in info.get('result', [])
        for addr in iface.get('ip-addresses', [])
        if addr.get('ip-address-type') == 'ipv6'
        and not str(addr.get('ip-address', '')).lower().startswith('fe80:')
    ]
    return ips, ips6


def _verify_linux_hostname(vmid, expected):
    out = run_command_in_guest(vmid, '/bin/hostname')
    actual = (out or '').strip().split('.')[0].lower()
    expected_l = str(expected).strip().split('.')[0].lower()
    return actual, actual == expected_l


def _verify_mount(vmid, mountpoint):
    """Return True when findmnt reports an exact mountpoint (not a parent FS)."""
    mp = str(mountpoint).rstrip('/') or '/'
    if mp == '/':
        return True
    cmd = f"/bin/findmnt -n -o TARGET --mountpoint {mp}"
    try:
        out = (run_command_in_guest(vmid, cmd) or '').strip()
    except Exception:  # noqa: BLE001
        return False
    return out == mp


def _linux_data_disk_script(serial: str, mountpoint: str, fstype: str) -> str:
    """Shell that finds a disk by serial, formats if needed, and mounts it."""
    mp = mountpoint or '/data'
    fs = fstype or 'ext4'
    # --mountpoint requires the path itself to be a mount; --target would
    # succeed for an unmounted directory that lives on the root FS.
    if fs == 'xfs':
        mkfs_cmd = f"mkfs.xfs -L '{serial}' -f \"$DEV\""
    else:
        mkfs_cmd = f"mkfs.{fs} -L '{serial}' -F \"$DEV\""
    return f"""
set -e
DEV=""
for p in /dev/disk/by-id/*{serial}* /dev/disk/by-id/scsi-{serial} /dev/disk/by-id/virtio-{serial}; do
  if [ -e "$p" ]; then DEV="$p"; break; fi
done
if [ -z "$DEV" ]; then
  echo "device for serial {serial} not found" >&2
  exit 1
fi
if findmnt -n --mountpoint '{mp}' >/dev/null 2>&1; then
  echo already_mounted
  exit 0
fi
mkdir -p '{mp}'
if ! blkid "$DEV" >/dev/null 2>&1; then
  {mkfs_cmd}
fi
mount "$DEV" '{mp}'
if ! grep -q "LABEL={serial}" /etc/fstab 2>/dev/null; then
  echo "LABEL={serial} {mp} {fs} defaults,nofail 0 2" >> /etc/fstab
fi
echo ok
"""


def finalize_linux_data_disks(vmid, disk_guest_plan):
    """Format/mount data+swap disks by serial via QGA (advanced path).

    Used when cloud-init cicustom snippets are not configured. Idempotent-ish:
    skips mkfs when already mounted.
    """
    import base64

    plan = disk_guest_plan or []
    for d in plan:
        role = d.get('role')
        serial = d.get('serial')
        if role == 'os' or not serial:
            continue
        if role == 'data':
            script = _linux_data_disk_script(
                serial,
                d.get('mountpoint') or '/data',
                d.get('fstype') or 'ext4',
            )
        elif role == 'swap':
            script = f"""
set -e
DEV=""
for p in /dev/disk/by-id/*{serial}* /dev/disk/by-id/scsi-{serial} /dev/disk/by-id/virtio-{serial}; do
  if [ -e "$p" ]; then DEV="$p"; break; fi
done
if [ -z "$DEV" ]; then
  echo "device for serial {serial} not found" >&2
  exit 1
fi
if swapon --show=NAME --noheadings 2>/dev/null | grep -q .; then
  echo swap_present
fi
if ! blkid "$DEV" >/dev/null 2>&1; then
  mkswap -L '{serial}' "$DEV"
fi
swapon "$DEV" || true
if ! grep -q "LABEL={serial}" /etc/fstab 2>/dev/null; then
  echo "LABEL={serial} none swap sw,nofail 0 0" >> /etc/fstab
fi
echo ok
"""
        else:
            continue
        b64 = base64.b64encode(script.encode()).decode()
        run_command_in_guest(
            vmid,
            f"/bin/bash -c 'echo {b64} | base64 -d | bash'",
            retries=4,
            retry_delay=10,
        )


def verify_linux_result(vmid, data, timeout=600, on_progress=None):
    """Return ``(summary, ok)`` after cloud-init network/hostname settle."""
    expected_hostname = data.get('hostname')
    expected_ip = None if data.get('use_dhcp') else data.get('ip_address')
    expected_ipv6 = data.get('ipv6_address') if data.get('enable_ipv6') else None
    disk_plan = data.get('disk_guest_plan') or []

    def _progress(msg):
        if on_progress:
            on_progress(msg)

    poll = 15
    polls = max(1, timeout // poll)
    found_ip = None
    found_ipv6 = None
    actual_hostname = None
    host_ok = False

    for i in range(polls):
        try:
            _progress(f'Waiting for Linux guest agent network ({i + 1}/{polls})...')
            actual_hostname, host_ok = _verify_linux_hostname(vmid, expected_hostname)
            ips, ips6 = _guest_ipv4_ipv6(vmid)
            if expected_ip:
                for cand in ips:
                    if cand == expected_ip:
                        found_ip = cand
                        break
            else:
                found_ip = ips[0] if ips else None
            if expected_ipv6:
                exp6 = str(expected_ipv6).lower()
                for cand in ips6:
                    if str(cand).lower() == exp6 or str(cand).lower().startswith(exp6):
                        found_ipv6 = cand
                        break
            else:
                found_ipv6 = True

            ip_ok = (found_ip is not None) if (expected_ip or not data.get('use_dhcp')) else True
            if data.get('use_dhcp'):
                ip_ok = found_ip is not None
            v6_ok = (found_ipv6 is not None) if expected_ipv6 else True
            if host_ok and ip_ok and v6_ok:
                break
        except Exception as e:  # noqa: BLE001
            app.logger.warning('Linux verify poll failed for VM %s: %s', vmid, type(e).__name__)
        if i + 1 < polls:
            time.sleep(poll)

    parts = []
    ok = True
    if host_ok:
        parts.append(f'hostname {actual_hostname}')
    else:
        ok = False
        parts.append(
            f'hostname mismatch (expected {expected_hostname}, got {actual_hostname!r})'
        )

    if data.get('use_dhcp'):
        if found_ip:
            parts.append(f'DHCP {found_ip}')
        else:
            ok = False
            parts.append('no DHCP IPv4 lease observed')
    elif expected_ip:
        if found_ip:
            parts.append(f'static {found_ip}')
        else:
            ok = False
            parts.append(f'static IPv4 {expected_ip} not observed')

    if expected_ipv6:
        if found_ipv6:
            parts.append(f'IPv6 {expected_ipv6}')
        else:
            ok = False
            parts.append(f'IPv6 {expected_ipv6} not observed')

    # Advanced disks: finalize via QGA then check mounts.
    data_mounts = [d for d in disk_plan if d.get('role') == 'data']
    if data_mounts:
        try:
            _progress('Formatting/mounting Linux data disks via guest agent...')
            finalize_linux_data_disks(vmid, disk_plan)
        except Exception as e:  # noqa: BLE001
            ok = False
            parts.append(f'disk finalize failed ({type(e).__name__})')
        for d in data_mounts:
            mp = d.get('mountpoint') or '/data'
            if _verify_mount(vmid, mp):
                parts.append(f'mount {mp}')
            else:
                ok = False
                parts.append(f'mount {mp} missing')

    packages = data.get('packages') or []
    if ok and packages:
        try:
            _progress('Installing apt packages...')
            install_linux_packages(vmid, packages)
            parts.append('packages ' + ' '.join(packages))
        except Exception as e:  # noqa: BLE001
            ok = False
            parts.append(f'package install failed ({type(e).__name__})')

    return '; '.join(parts), ok


def install_linux_packages(vmid, packages):
    """Install already-validated apt names. The names cannot carry shell syntax."""
    names = ' '.join(packages)
    command = (
        "sh -c 'export DEBIAN_FRONTEND=noninteractive; "
        f"apt-get update && apt-get install -y --no-install-recommends {names}'"
    )
    run_command_in_guest(vmid, command, retries=2, retry_delay=10)
