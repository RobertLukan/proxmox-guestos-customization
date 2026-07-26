"""Short-lived HMAC launch tokens for PDM → GuestOS one-click session.

PDM signs ``exp.template_vmid.remote_id`` with ``GUESTOS_LAUNCH_SECRET``; GuestOS
verifies and creates a browser session so the operator skips the password form.
Tokens are single-use (jti recorded) and expire quickly (default 5 minutes).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from threading import Lock

from app import app

# In-process used JTIs (Compose web is typically one worker; good enough for lab).
_used_jtis: set[str] = set()
_used_lock = Lock()
_MAX_USED = 4096


def launch_secret() -> str:
    return (app.config.get('GUESTOS_LAUNCH_SECRET') or '').strip()


def launch_ttl_seconds() -> int:
    try:
        return max(60, int(app.config.get('GUESTOS_LAUNCH_TTL') or 300))
    except (TypeError, ValueError):
        return 300


def _canonical(exp: int, template_vmid: str, remote_id: str, jti: str) -> str:
    return f'{int(exp)}.{template_vmid}.{remote_id}.{jti}'


def sign_launch_token(template_vmid, remote_id: str = '', ttl: int | None = None) -> dict:
    """Return dict with exp, jti, sig for URL query params."""
    secret = launch_secret()
    if not secret:
        raise RuntimeError('GUESTOS_LAUNCH_SECRET is not configured')
    ttl = launch_ttl_seconds() if ttl is None else max(60, int(ttl))
    exp = int(time.time()) + ttl
    jti = secrets.token_hex(8)
    vmid = str(template_vmid).strip()
    remote = (remote_id or '').strip()
    msg = _canonical(exp, vmid, remote, jti)
    sig = hmac.new(secret.encode('utf-8'), msg.encode('utf-8'), hashlib.sha256).hexdigest()
    return {'exp': exp, 'jti': jti, 'sig': sig, 'template_vmid': vmid, 'remote_id': remote}


def verify_launch_token(exp, template_vmid, remote_id, jti, sig) -> tuple[bool, str]:
    """Return (ok, error_message). On success marks ``jti`` consumed."""
    secret = launch_secret()
    if not secret:
        return False, 'Launch tokens are not configured on this GuestOS instance.'
    try:
        exp_i = int(exp)
    except (TypeError, ValueError):
        return False, 'Invalid exp.'
    if exp_i < int(time.time()):
        return False, 'Launch token expired.'
    vmid = str(template_vmid or '').strip()
    remote = str(remote_id or '').strip()
    jti_s = str(jti or '').strip()
    sig_s = str(sig or '').strip().lower()
    if not vmid or not jti_s or not sig_s:
        return False, 'Missing launch token fields.'
    if len(jti_s) > 64 or len(sig_s) != 64:
        return False, 'Malformed launch token.'
    msg = _canonical(exp_i, vmid, remote, jti_s)
    expected = hmac.new(secret.encode('utf-8'), msg.encode('utf-8'), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig_s):
        return False, 'Invalid launch token signature.'
    with _used_lock:
        if jti_s in _used_jtis:
            return False, 'Launch token already used.'
        _used_jtis.add(jti_s)
        if len(_used_jtis) > _MAX_USED:
            # Drop arbitrary half to bound memory (lab-scale).
            for _ in range(_MAX_USED // 2):
                _used_jtis.pop()
    return True, ''
