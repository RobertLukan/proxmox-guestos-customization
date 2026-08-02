"""Short-TTL stash for sysprep secrets kept out of Celery/Redis payloads.

Routes call :func:`stash_task_secrets` before ``apply_async`` so
``administrator_password`` / ``domain_password`` never ride the broker.
Workers call :func:`load_task_secrets` at task start. After files are written,
:func:`scrub_workflow_secrets` drops residual secrets before verify enqueue.
"""
from __future__ import annotations

import json
import logging
import time

from app import app

_log = logging.getLogger(__name__)

_SECRET_KEYS = ('administrator_password', 'domain_password')
_VERIFY_SCRUB_KEYS = (
    'administrator_password',
    'domain_password',
    'domain_join_b64',
)
_TTL_SECONDS = 6 * 3600  # covers long Sysprep + verify windows


def _redis_client():
    url = (app.config.get('CELERY_BROKER_URL') or '').strip()
    if not url.startswith('redis'):
        return None
    try:
        import redis  # type: ignore

        return redis.Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)
    except Exception as e:  # noqa: BLE001
        _log.warning('Task secrets Redis unavailable: %s', e)
        return None


def _stash_redis(task_id: str, secrets: dict) -> bool:
    client = _redis_client()
    if client is None:
        return False
    try:
        key = f'guestos:task:secrets:{task_id}'
        client.set(key, json.dumps(secrets), ex=_TTL_SECONDS)
        return True
    except Exception as e:  # noqa: BLE001
        _log.warning('Task secrets Redis stash failed: %s', e)
        return False


def _load_redis(task_id: str) -> dict | None:
    client = _redis_client()
    if client is None:
        return None
    try:
        key = f'guestos:task:secrets:{task_id}'
        raw = client.get(key)
        if raw is None:
            return {}
        client.delete(key)
        return json.loads(raw)
    except Exception as e:  # noqa: BLE001
        _log.warning('Task secrets Redis load failed: %s', e)
        return None


def _stash_sqlite(task_id: str, secrets: dict) -> bool:
    try:
        from sqlalchemy import text
        from app import db

        db.session.execute(
            text(
                'CREATE TABLE IF NOT EXISTS task_secrets ('
                'task_id VARCHAR(64) PRIMARY KEY, '
                'payload TEXT NOT NULL, '
                'exp INTEGER NOT NULL)'
            )
        )
        db.session.execute(
            text('DELETE FROM task_secrets WHERE exp < :now'),
            {'now': int(time.time())},
        )
        db.session.execute(
            text(
                'INSERT OR REPLACE INTO task_secrets (task_id, payload, exp) '
                'VALUES (:task_id, :payload, :exp)'
            ),
            {
                'task_id': task_id,
                'payload': json.dumps(secrets),
                'exp': int(time.time()) + _TTL_SECONDS,
            },
        )
        db.session.commit()
        return True
    except Exception as e:  # noqa: BLE001
        try:
            from app import db

            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        _log.warning('Task secrets SQLite stash failed: %s', e)
        return False


def _load_sqlite(task_id: str) -> dict | None:
    try:
        from sqlalchemy import text
        from app import db

        row = db.session.execute(
            text('SELECT payload, exp FROM task_secrets WHERE task_id = :task_id'),
            {'task_id': task_id},
        ).first()
        db.session.execute(
            text('DELETE FROM task_secrets WHERE task_id = :task_id'),
            {'task_id': task_id},
        )
        db.session.commit()
        if not row:
            return {}
        payload, exp = row[0], int(row[1])
        if exp < int(time.time()):
            return {}
        return json.loads(payload)
    except Exception as e:  # noqa: BLE001
        try:
            from app import db

            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        _log.warning('Task secrets SQLite load failed: %s', e)
        return None


def stash_task_secrets(task_id: str, data: dict) -> None:
    """Move sensitive fields from ``data`` into a short-TTL side channel."""
    secrets = {}
    for key in _SECRET_KEYS:
        val = data.pop(key, None)
        if val not in (None, ''):
            secrets[key] = val
    if not secrets:
        return
    if _stash_redis(task_id, secrets):
        return
    if _stash_sqlite(task_id, secrets):
        return
    # Last resort: put secrets back so the job can still run (lab without Redis).
    _log.error(
        'Could not stash task secrets for %s; restoring onto payload '
        '(configure Redis for production).',
        task_id,
    )
    data.update(secrets)


def load_task_secrets(task_id: str, data: dict) -> None:
    """Merge stashed secrets into ``data`` (one-shot consume)."""
    secrets = _load_redis(task_id)
    if secrets is None:
        secrets = _load_sqlite(task_id)
    if not secrets:
        return
    for key, val in secrets.items():
        if key in _SECRET_KEYS and val not in (None, ''):
            data[key] = val


def scrub_workflow_secrets(data: dict) -> None:
    """Drop residual secrets before verify-queue enqueue."""
    for key in _VERIFY_SCRUB_KEYS:
        data.pop(key, None)
