"""Disk plan validation and normalization for optional Sysprep disk reconcile."""
from __future__ import annotations

import re

from app.validators import ValidationError

DISK_ROLES = frozenset({'os', 'data', 'pagefile'})
DRIVE_LETTER_RE = re.compile(r'^[A-Za-z]$')

# Proxmox disk option keys safe to copy from the boot disk onto new volumes.
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


def _as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('true', '1', 't', 'yes', 'on')


def _int_gb(value, field):
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValidationError(f'{field} must be an integer number of GB.')
    if n < 1 or n > 65536:
        raise ValidationError(f'{field} out of range 1–65536 GB: {n}')
    return n


def _drive_letter(value, field='drive_letter'):
    if value in (None, ''):
        return None
    v = str(value).strip().upper()
    if len(v) == 2 and v.endswith(':'):
        v = v[0]
    if not DRIVE_LETTER_RE.match(v) or v in ('C',):
        raise ValidationError(f'{field} must be a single letter A–Z except C.')
    return v


def prepare_disk_plan(data):
    """Normalize ``manage_disks`` / ``disks`` on the workflow payload.

    When manage_disks is false, clears disk work and returns early.
    Mutates ``data`` in place. Raises ValidationError on bad input.
    """
    manage = _as_bool(data.get('manage_disks'), False)
    data['manage_disks'] = manage
    if not manage:
        if data.get('disks'):
            # Prefer ignore (plan): do not fail callers that send unused disks.
            pass
        data['disks'] = []
        data['disk_guest_plan'] = []
        return

    raw = data.get('disks')
    if raw in (None, '', []):
        # Sensible default plan when the checkbox is on but the list is empty.
        raw = [
            {'role': 'os'},
            {'role': 'pagefile', 'size_gb': 16, 'drive_letter': 'P', 'ensure_pagefile': True},
            {'role': 'data', 'size_gb': 50, 'drive_letter': 'D', 'label': 'Data'},
        ]

    if not isinstance(raw, list):
        raise ValidationError('disks must be a list of disk plan entries.')

    normalized = []
    letters_seen = set()
    os_count = 0
    pagefile_count = 0

    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValidationError(f'disks[{i}] must be an object.')
        role = str(entry.get('role') or '').strip().lower()
        if role not in DISK_ROLES:
            raise ValidationError(f'disks[{i}].role must be one of {sorted(DISK_ROLES)}.')

        item = {
            'role': role,
            'reformat': _as_bool(
                entry.get('reformat'),
                # Pagefile volumes should be dedicated; template leftovers (wrong
                # letter/size) are common when reusing a secondary disk.
                True if str(entry.get('role', '')).lower() == 'pagefile' else False,
            ),
            'label': (str(entry.get('label') or '').strip() or None),
        }

        if role == 'os':
            os_count += 1
            if entry.get('grow_to_gb') not in (None, ''):
                item['grow_to_gb'] = _int_gb(entry.get('grow_to_gb'), 'grow_to_gb')
            if entry.get('min_size_gb') not in (None, ''):
                item['min_size_gb'] = _int_gb(entry.get('min_size_gb'), 'min_size_gb')
            if item.get('grow_to_gb') and item.get('min_size_gb'):
                if item['grow_to_gb'] < item['min_size_gb']:
                    raise ValidationError('os grow_to_gb must be >= min_size_gb.')
        else:
            if entry.get('size_gb') in (None, ''):
                raise ValidationError(f'disks[{i}] ({role}) requires size_gb.')
            item['size_gb'] = _int_gb(entry.get('size_gb'), 'size_gb')
            item['min_size_gb'] = item['size_gb']
            letter = _drive_letter(entry.get('drive_letter'))
            if not letter:
                letter = 'P' if role == 'pagefile' else 'D'
            if letter in letters_seen:
                raise ValidationError(f'Duplicate drive_letter {letter}.')
            letters_seen.add(letter)
            item['drive_letter'] = letter
            if role == 'pagefile':
                pagefile_count += 1
                item['ensure_pagefile'] = _as_bool(entry.get('ensure_pagefile'), True)
            else:
                item['ensure_pagefile'] = False

        # Stable serial for guest matching (PVE disk serial=…).
        if role == 'os':
            item['serial'] = 'guestos-os'
        elif role == 'pagefile':
            item['serial'] = 'guestos-pagefile'
        else:
            data_idx = sum(1 for x in normalized if x['role'] == 'data')
            item['serial'] = f'guestos-data-{data_idx}'

        normalized.append(item)

    if os_count != 1:
        raise ValidationError('disks plan must include exactly one role=os entry.')
    if pagefile_count > 1:
        raise ValidationError('disks plan may include at most one role=pagefile entry.')

    data['disks'] = normalized
    # Guest plan filled after PVE reconcile (serials confirmed / assigned).
    data['disk_guest_plan'] = []
