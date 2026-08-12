"""Linux disk plan validation (cloud-init / QGA path — not Windows manage_disks)."""
from __future__ import annotations

import re

from app.util import as_bool as _as_bool
from app.validators import ValidationError

DISK_ROLES = frozenset({'os', 'data', 'swap'})
SOURCE_KEY_RE = re.compile(r'^(scsi|virtio|sata|ide)\d+$')
MOUNT_RE = re.compile(r'^/(?:[A-Za-z0-9._-]+/?)*$')
FSTYPES = frozenset({'ext4', 'xfs', 'swap'})

COPYABLE_DISK_OPTS = frozenset({
    'aio',
    'discard',
    'cache',
    'iothread',
    'ssd',
    'backup',
    'replicate',
    'detect_zeroes',
    'queue-size',
    'blocksize',
})


def _int_gb(value, field):
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValidationError(f'{field} must be an integer number of GB.')
    if n < 1 or n > 65536:
        raise ValidationError(f'{field} out of range 1–65536 GB: {n}')
    return n


def _mountpoint(value, field='mountpoint'):
    if value in (None, ''):
        return None
    v = str(value).strip()
    if v != '/' and not MOUNT_RE.match(v.rstrip('/') or '/'):
        raise ValidationError(f'{field} must be an absolute path: {value!r}')
    if v != '/':
        v = v.rstrip('/') or '/'
    return v


def prepare_linux_disk_plan(data):
    """Normalize ``manage_disks`` / ``disks`` for Linux cloud-init customize.

    Default (Simple): manage_disks false — one large root, optional OS grow only
    via a single os row is still allowed when manage_disks is true.
    Mutates ``data`` in place.
    """
    manage = _as_bool(data.get('manage_disks'), False)
    data['manage_disks'] = manage
    if not manage:
        data['disks'] = []
        data['disk_guest_plan'] = []
        return

    raw = data.get('disks')
    if raw in (None, '', []):
        raise ValidationError(
            'disks plan is required when manage_disks is enabled '
            '(include role=os and optional data/swap entries).'
        )
    if not isinstance(raw, list):
        raise ValidationError('disks must be a list of disk plan entries.')

    normalized = []
    source_keys_seen = set()
    mounts_seen = set()
    os_count = 0
    swap_count = 0

    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValidationError(f'disks[{i}] must be an object.')
        role = str(entry.get('role') or '').strip().lower()
        if role not in DISK_ROLES:
            raise ValidationError(f'disks[{i}].role must be one of {sorted(DISK_ROLES)}.')

        item = {
            'role': role,
            'reformat': _as_bool(entry.get('reformat'), False),
            'label': (str(entry.get('label') or '').strip() or None),
        }

        source_key = entry.get('source_key')
        if source_key not in (None, ''):
            sk = str(source_key).strip().lower()
            if not SOURCE_KEY_RE.match(sk):
                raise ValidationError(
                    f'disks[{i}].source_key must be a bus disk key '
                    f'(scsiN / virtioN / sataN / ideN).'
                )
            if sk in source_keys_seen:
                raise ValidationError(f'Duplicate source_key {sk}.')
            source_keys_seen.add(sk)
            item['source_key'] = sk

        if role == 'os':
            os_count += 1
            if entry.get('grow_to_gb') not in (None, ''):
                item['grow_to_gb'] = _int_gb(entry.get('grow_to_gb'), 'grow_to_gb')
            if entry.get('min_size_gb') not in (None, ''):
                item['min_size_gb'] = _int_gb(entry.get('min_size_gb'), 'min_size_gb')
            if item.get('grow_to_gb') and item.get('min_size_gb'):
                if item['grow_to_gb'] < item['min_size_gb']:
                    raise ValidationError('os grow_to_gb must be >= min_size_gb.')
            item['serial'] = 'guestos-os'
            item['fstype'] = 'ext4'
            item['mountpoint'] = '/'
        elif role == 'swap':
            swap_count += 1
            item['size_gb'] = _int_gb(entry.get('size_gb'), 'size_gb')
            item['serial'] = f'guestos-swap{swap_count}'
            item['fstype'] = 'swap'
            item['mountpoint'] = 'none'
            item['use_swapfile'] = False
        else:  # data
            item['size_gb'] = _int_gb(entry.get('size_gb'), 'size_gb')
            mp = _mountpoint(entry.get('mountpoint') or '/data', 'mountpoint')
            if mp in mounts_seen:
                raise ValidationError(f'Duplicate mountpoint {mp}.')
            mounts_seen.add(mp)
            item['mountpoint'] = mp
            fstype = str(entry.get('fstype') or 'ext4').strip().lower()
            if fstype not in FSTYPES or fstype == 'swap':
                raise ValidationError(f'disks[{i}].fstype must be ext4 or xfs.')
            item['fstype'] = fstype
            idx = sum(1 for x in normalized if x['role'] == 'data') + 1
            item['serial'] = f'guestos-data{idx}'

        normalized.append(item)

    if os_count != 1:
        raise ValidationError('Exactly one disks entry with role=os is required.')
    if swap_count > 1:
        raise ValidationError('At most one swap disk is allowed.')

    data['disks'] = normalized
    # Serials assigned; guest plan filled after PVE reconcile.
    data['disk_guest_plan'] = []


def reconcile_linux_vm_disks(vmid, disks_plan):
    """Attach/grow Linux disks per plan. Returns guest_plan for verify / QGA finalize.

    Mirrors Windows ``reconcile_vm_disks`` but emits mountpoint/fstype instead of
    drive letters. OS grow relies on guest cloud-init growpart on first boot.
    """
    from app.proxmox import (
        _format_disk_value,
        _pick_non_boot_disk,
        _resize_disk_to_gb,
        _set_disk_serial,
        get_boot_disk_spec,
    )

    boot = get_boot_disk_spec(vmid)
    proxmox, node = boot['proxmox'], boot['node']
    disks = list(boot['disks'])
    copy_opts = {k: v for k, v in boot['opts'].items() if k in COPYABLE_DISK_OPTS}
    guest_plan = []
    bus = boot['bus']
    used_indices = {d['index'] for d in disks if d['bus'] == bus}

    def _alloc_bus_key():
        n = 0
        while n in used_indices:
            n += 1
        used_indices.add(n)
        return f'{bus}{n}'

    non_boot = [d for d in disks if d['key'] != boot['key']]

    for item in disks_plan:
        role = item['role']
        serial = item['serial']

        if role == 'os':
            target = item.get('grow_to_gb') or item.get('min_size_gb')
            grown = False
            if target:
                grown = _resize_disk_to_gb(
                    proxmox, node, vmid, boot['key'], boot.get('size_gb'), target
                )
            _set_disk_serial(proxmox, node, vmid, boot['key'], boot['raw'], serial)
            guest_plan.append({
                'role': 'os',
                'serial': serial,
                'mountpoint': '/',
                'fstype': item.get('fstype') or 'ext4',
                'min_size_gb': item.get('min_size_gb') or target or boot.get('size_gb') or 1,
                'reformat': False,
                'extend': bool(grown or item.get('grow_to_gb')),
                'label': item.get('label'),
                'pve_key': boot['key'],
            })
            continue

        matched = _pick_non_boot_disk(non_boot, item)
        size_gb = int(item['size_gb'])
        if matched:
            existing_serial = str(matched.get('serial') or '')
            if (
                existing_serial
                and existing_serial != str(serial)
                and not bool(item.get('reformat'))
            ):
                raise Exception(
                    f"Refusing to overwrite disk {matched['key']} "
                    f"(serial={existing_serial}) with role={role} serial={serial} "
                    f"without reformat=true."
                )
            _set_disk_serial(
                proxmox, node, vmid, matched['key'], matched['raw'], serial,
                extra_opts=copy_opts,
            )
            cur = matched.get('size_gb')
            if cur is not None and size_gb > cur:
                _resize_disk_to_gb(proxmox, node, vmid, matched['key'], cur, size_gb)
            pve_key = matched['key']
            used_indices.add(matched['index'])
        else:
            pve_key = _alloc_bus_key()
            value = _format_disk_value(boot['storage'], size_gb, copy_opts, serial=serial)
            proxmox.nodes(node).qemu(vmid).config.post(**{pve_key: value})

        guest_plan.append({
            'role': role,
            'serial': serial,
            'mountpoint': item.get('mountpoint') or ('none' if role == 'swap' else '/data'),
            'fstype': item.get('fstype') or ('swap' if role == 'swap' else 'ext4'),
            'min_size_gb': size_gb,
            'reformat': bool(item.get('reformat')),
            'extend': True,
            'label': item.get('label'),
            'pve_key': pve_key,
        })

    return guest_plan
