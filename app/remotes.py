"""Resolve optional ``remote_id`` to Proxmox connection overrides for tasks."""
from __future__ import annotations

from app import app
from app.util import as_bool


def lookup_pve_remote(remote_id: str) -> dict:
    """Return server-side PVE override dict for ``remote_id``.

    Raises ``ValueError`` when the remote is unknown or incomplete.
    """
    remotes = app.config.get('PVE_REMOTES') or {}
    remote = remotes.get(remote_id)
    if not remote:
        raise ValueError(f'Unknown remote_id: {remote_id!r}')

    host = (remote.get('host') or remote.get('proxmox_host') or '').strip()
    user = (remote.get('user') or remote.get('proxmox_user') or '').strip()
    password = remote.get('password') or remote.get('proxmox_password') or ''
    if not host or not user or password == '':
        raise ValueError(
            f'Remote {remote_id!r} is missing host, user, or password in config.'
        )

    verify = remote.get('verify_ssl', remote.get('proxmox_verify_ssl'))
    if verify is None:
        verify = app.config.get('PROXMOX_VERIFY_SSL', False)

    return {
        'host': host,
        'user': user,
        'password': password,
        'verify_ssl': as_bool(verify, False),
    }


def resolve_pve_remote(data, field_error_fn):
    """Validate ``remote_id`` for the HTTP start path without attaching secrets.

    Credentials are resolved later inside the Celery worker via
    :func:`attach_pve_override` so PVE passwords never enter Redis/Celery
    payloads. Empty ``remote_id`` keeps the default ``PROXMOX_*`` config.

    Returns ``(True, None)`` or ``(False, (response, status))``.
    """
    remote_id = (data.get('remote_id') or '').strip()
    data.pop('_pve', None)
    if not remote_id:
        data.pop('remote_id', None)
        return True, None

    try:
        lookup_pve_remote(remote_id)
    except ValueError as e:
        msg = str(e)
        return False, field_error_fn(msg, remote_id=msg)

    data['remote_id'] = remote_id
    return True, None


def attach_pve_override(data) -> dict | None:
    """Resolve ``remote_id`` to a PVE override for the worker (or None).

    Does not mutate ``data`` with secrets — returns the override for
    ``use_pve_override``.
    """
    remote_id = (data.get('remote_id') or '').strip()
    if not remote_id:
        return None
    return lookup_pve_remote(remote_id)
