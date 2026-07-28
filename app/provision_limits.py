"""Provisioning quotas and resource caps (PVE / storage safeguards)."""
from __future__ import annotations

from datetime import timedelta

from flask import current_app

from app.models import Task, _utcnow
from app.util import as_bool as _as_bool
from app.validators import ValidationError

PVE_ADMIN_HINT = (
    'Additional VMs or larger resources must be configured directly in '
    'Proxmox VE by an administrator.'
)


def _cfg(key, default):
    try:
        return current_app.config.get(key, default)
    except RuntimeError:
        return default


def family_caps(family):
    """Return max cores / RAM MB / disk GB for ``win11`` or ``server``."""
    fam = (family or 'win11').strip().lower()
    if fam == 'server':
        return {
            'family': 'server',
            'max_cores': int(_cfg('SERVER_MAX_CORES', 16)),
            'max_ram_mb': int(_cfg('SERVER_MAX_RAM_MB', 65536)),
            'max_disk_gb': int(_cfg('SERVER_MAX_DISK_GB', 2048)),
        }
    return {
        'family': 'win11',
        'max_cores': int(_cfg('WIN11_MAX_CORES', 8)),
        'max_ram_mb': int(_cfg('WIN11_MAX_RAM_MB', 65536)),
        'max_disk_gb': int(_cfg('WIN11_MAX_DISK_GB', 600)),
    }


def requested_disk_gb(payload):
    """Sum requested disk GB from a sysprep payload (manage_disks plan only)."""
    if not _as_bool((payload or {}).get('manage_disks'), False):
        return 0
    disks = (payload or {}).get('disks') or []
    if not isinstance(disks, list):
        return 0
    total = 0
    for entry in disks:
        if not isinstance(entry, dict):
            continue
        for key in ('grow_to_gb', 'size_gb', 'min_size_gb'):
            val = entry.get(key)
            if val in (None, ''):
                continue
            try:
                total += int(val)
            except (TypeError, ValueError):
                continue
            break
    return total


def validate_resource_caps(payload, family):
    """Raise ValidationError when cores/RAM/disks exceed family ceilings."""
    caps = family_caps(family)
    label = 'Windows Server' if caps['family'] == 'server' else 'Windows 11'

    try:
        cores = int((payload or {}).get('cores'))
    except (TypeError, ValueError):
        raise ValidationError('cores must be an integer.')
    if cores < 1:
        raise ValidationError('cores must be at least 1.')
    if cores > caps['max_cores']:
        raise ValidationError(
            f'{label} core limit is {caps["max_cores"]} (requested {cores}). {PVE_ADMIN_HINT}'
        )

    try:
        ram = int((payload or {}).get('ram'))
    except (TypeError, ValueError):
        raise ValidationError('ram must be an integer number of MB.')
    if ram < 512:
        raise ValidationError('ram must be at least 512 MB.')
    if ram > caps['max_ram_mb']:
        raise ValidationError(
            f'{label} RAM limit is {caps["max_ram_mb"]} MB '
            f'(requested {ram} MB). {PVE_ADMIN_HINT}'
        )

    disk_gb = requested_disk_gb(payload)
    if disk_gb > caps['max_disk_gb']:
        raise ValidationError(
            f'{label} disk limit is {caps["max_disk_gb"]} GB total '
            f'(requested {disk_gb} GB). {PVE_ADMIN_HINT}'
        )
    return caps


def daily_provision_count(since=None):
    """Count Task rows created since ``since`` (default: rolling 24h)."""
    since = since or (_utcnow() - timedelta(hours=24))
    return Task.query.filter(Task.timestamp >= since).count()


def check_daily_quota(extra_items=1):
    """Raise ValidationError when daily provisioning quota would be exceeded."""
    max_day = int(_cfg('PROVISION_MAX_PER_DAY', 20))
    used = daily_provision_count()
    extra = max(0, int(extra_items or 0))
    if used + extra > max_day:
        raise ValidationError(
            f'Daily provisioning limit reached ({used}/{max_day} in 24h; '
            f'requested {extra} more). {PVE_ADMIN_HINT}'
        )
    return {
        'daily_max': max_day,
        'daily_used': used,
        'daily_remaining': max(0, max_day - used),
    }


def evaluate_storage_usage(used_pct):
    """Return ``(level, message)`` for storage used%.

    Levels: ``ok``, ``warn`` (>= warn threshold), ``block`` (>= block threshold).
    """
    warn_at = float(_cfg('STORAGE_WARN_PCT', 65))
    block_at = float(_cfg('STORAGE_BLOCK_PCT', 80))
    pct = float(used_pct or 0)
    if pct >= block_at:
        return (
            'block',
            f'Storage is {pct:.1f}% used (block at {block_at:.0f}%). '
            f'Deployment refused. {PVE_ADMIN_HINT}',
        )
    if pct >= warn_at:
        return (
            'warn',
            f'Storage is {pct:.1f}% used (warning at {warn_at:.0f}%). '
            'Consider freeing space before large batches.',
        )
    return ('ok', '')


def check_storage_for_template(template_vmid, get_usage=None):
    """Inspect template boot-disk storage.

    Returns ``(level, details_dict)``. Raises ValidationError on ``block``.
    ``get_usage`` is injectable for tests (returns same shape as
    ``get_template_storage_usage``).
    """
    if get_usage is None:
        from app.proxmox import get_template_storage_usage
        get_usage = get_template_storage_usage
    try:
        usage = get_usage(template_vmid) or {}
    except Exception as e:
        # Fail open with a warning rather than blocking when status is unavailable.
        return (
            'warn',
            {
                'storage': None,
                'node': None,
                'used_pct': None,
                'level': 'warn',
                'message': f'Could not read Proxmox storage usage: {e}',
            },
        )
    level, message = evaluate_storage_usage(usage.get('used_pct') or 0)
    details = {
        'storage': usage.get('storage'),
        'node': usage.get('node'),
        'used_pct': usage.get('used_pct'),
        'used': usage.get('used'),
        'total': usage.get('total'),
        'avail': usage.get('avail'),
        'level': level,
        'message': message,
    }
    if level == 'block':
        raise ValidationError(message)
    return level, details


def provision_limits_snapshot(family='win11', template_vmid=None, get_usage=None):
    """Operator-facing snapshot of caps, remaining quotas, and storage."""
    caps = family_caps(family)
    max_day = int(_cfg('PROVISION_MAX_PER_DAY', 20))
    used = daily_provision_count()
    daily_remaining = max(0, max_day - used)
    max_items = int(_cfg('BULK_MAX_ITEMS', 10))
    batch_remaining = min(max_items, daily_remaining)
    inflight_statuses = ('PENDING', 'STARTED', 'PROGRESS')
    inflight = Task.query.filter(Task.status.in_(inflight_statuses)).count()
    inflight_max = int(_cfg('BULK_MAX_CONCURRENT_GLOBAL', 10))

    storage = None
    if template_vmid not in (None, ''):
        try:
            _level, storage = check_storage_for_template(template_vmid, get_usage=get_usage)
        except ValidationError as e:
            storage = {
                'level': 'block',
                'message': str(e),
                'storage': None,
                'node': None,
                'used_pct': None,
            }

    return {
        **caps,
        'bulk_max_items': max_items,
        'bulk_allowed': caps['family'] == 'win11',
        'daily_max': max_day,
        'daily_used': used,
        'daily_remaining': daily_remaining,
        'batch_remaining': batch_remaining if caps['family'] == 'win11' else 0,
        'inflight_global': inflight,
        'inflight_global_max': inflight_max,
        'storage': storage,
        'admin_hint': PVE_ADMIN_HINT,
    }
