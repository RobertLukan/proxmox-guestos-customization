"""Resolve optional ``remote_id`` to Proxmox connection overrides for tasks."""
from __future__ import annotations

from app import app


def _as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('true', '1', 't', 'yes', 'on')


def resolve_pve_remote(data, field_error_fn):
    """Attach server-side ``_pve`` credentials when ``remote_id`` is set.

    Credentials always come from ``PVE_REMOTES`` config — never from the
    request body. Empty ``remote_id`` keeps the default ``PROXMOX_*`` config
    (standalone behaviour).

    Returns ``(True, None)`` or ``(False, (response, status))``.
    """
    remote_id = (data.get('remote_id') or '').strip()
    if not remote_id:
        data.pop('_pve', None)
        data.pop('remote_id', None)
        return True, None

    remotes = app.config.get('PVE_REMOTES') or {}
    remote = remotes.get(remote_id)
    if not remote:
        msg = f'Unknown remote_id: {remote_id!r}'
        return False, field_error_fn(msg, remote_id=msg)

    host = (remote.get('host') or remote.get('proxmox_host') or '').strip()
    user = (remote.get('user') or remote.get('proxmox_user') or '').strip()
    password = remote.get('password') or remote.get('proxmox_password') or ''
    if not host or not user or password == '':
        msg = f'Remote {remote_id!r} is missing host, user, or password in config.'
        return False, field_error_fn(msg, remote_id=msg)

    verify = remote.get('verify_ssl', remote.get('proxmox_verify_ssl'))
    if verify is None:
        verify = app.config.get('PROXMOX_VERIFY_SSL', False)

    data['remote_id'] = remote_id
    data['_pve'] = {
        'host': host,
        'user': user,
        'password': password,
        'verify_ssl': _as_bool(verify, False),
    }
    return True, None
