"""Short-lived HMAC launch tokens for PDM → GuestOS one-click session.

PDM signs ``exp.template_vmid.remote_id`` with ``GUESTOS_LAUNCH_SECRET``; GuestOS
verifies and creates a browser session so the operator skips the password form.
Tokens are single-use (jti recorded) and expire quickly (default 5 minutes).

JTI anti-replay prefers Redis (shared across web workers), then SQLite, then an
in-process set as last resort.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from threading import Lock

from app import app

_used_jtis: set[str] = set()
_used_lock = Lock()
_MAX_USED = 4096
_log = logging.getLogger(__name__)


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


def _consume_jti_redis(jti: str, exp: int) -> bool | None:
    """Return True if newly consumed, False if already used, None if Redis unavailable."""
    url = (app.config.get('CELERY_BROKER_URL') or '').strip()
    if not url.startswith('redis'):
        return None
    try:
        import redis  # type: ignore
        client = redis.Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)
        key = f'guestos:launch:jti:{jti}'
        ttl = max(60, int(exp) - int(time.time()) + 60)
        # SET NX — first consumer wins.
        created = client.set(key, '1', nx=True, ex=ttl)
        return bool(created)
    except Exception as e:  # noqa: BLE001
        _log.warning('Launch JTI Redis store unavailable: %s', e)
        return None


def _consume_jti_sqlite(jti: str, exp: int) -> bool | None:
    """Return True if newly consumed, False if already used, None on DB error."""
    try:
        from sqlalchemy import text
        from app import db

        db.session.execute(
            text(
                'CREATE TABLE IF NOT EXISTS launch_jti ('
                'jti VARCHAR(64) PRIMARY KEY, '
                'exp INTEGER NOT NULL, '
                'used_at REAL NOT NULL)'
            )
        )
        # Opportunistic cleanup of expired rows.
        db.session.execute(
            text('DELETE FROM launch_jti WHERE exp < :now'),
            {'now': int(time.time()) - 3600},
        )
        existing = db.session.execute(
            text('SELECT 1 FROM launch_jti WHERE jti = :jti'),
            {'jti': jti},
        ).first()
        if existing:
            db.session.commit()
            return False
        db.session.execute(
            text('INSERT INTO launch_jti (jti, exp, used_at) VALUES (:jti, :exp, :used_at)'),
            {'jti': jti, 'exp': int(exp), 'used_at': time.time()},
        )
        db.session.commit()
        return True
    except Exception as e:  # noqa: BLE001
        try:
            from app import db
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        _log.warning('Launch JTI SQLite store unavailable: %s', e)
        return None


def _consume_jti_memory(jti: str) -> bool:
    with _used_lock:
        if jti in _used_jtis:
            return False
        _used_jtis.add(jti)
        if len(_used_jtis) > _MAX_USED:
            for _ in range(_MAX_USED // 2):
                _used_jtis.pop()
        return True


def _consume_jti(jti: str, exp: int) -> bool:
    redis_result = _consume_jti_redis(jti, exp)
    if redis_result is not None:
        return redis_result
    sqlite_result = _consume_jti_sqlite(jti, exp)
    if sqlite_result is not None:
        return sqlite_result
    return _consume_jti_memory(jti)


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
    if not _consume_jti(jti_s, exp_i):
        return False, 'Launch token already used.'
    return True, ''
