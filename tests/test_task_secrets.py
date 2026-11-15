"""Tests for task secret side-channel (kept out of Celery payloads)."""

from app.task_secrets import load_task_secrets, scrub_workflow_secrets, stash_task_secrets


def test_stash_and_load_roundtrip(app):
    data = {
        'hostname': 'HOST1',
        'administrator_password': 'AdminSecret!',
        'domain_password': 'DomainSecret!',
    }
    with app.app_context():
        stash_task_secrets('task-secrets-1', data)
        assert 'administrator_password' not in data
        assert 'domain_password' not in data
        assert data['hostname'] == 'HOST1'

        restored = {'hostname': 'HOST1'}
        load_task_secrets('task-secrets-1', restored)
        assert restored['administrator_password'] == 'AdminSecret!'
        assert restored['domain_password'] == 'DomainSecret!'

        # One-shot consume.
        again = {}
        load_task_secrets('task-secrets-1', again)
        assert 'administrator_password' not in again


def test_scrub_workflow_secrets():
    data = {
        'administrator_password': 'x',
        'domain_password': 'y',
        'domain_join_b64': 'abc',
        'hostname': 'H',
    }
    scrub_workflow_secrets(data)
    assert 'administrator_password' not in data
    assert 'domain_password' not in data
    assert 'domain_join_b64' not in data
    assert data['hostname'] == 'H'


def test_stash_fail_closed_when_backends_unavailable(app, monkeypatch):
    from app import task_secrets as ts

    monkeypatch.setattr(ts, '_stash_redis', lambda *a, **k: False)
    monkeypatch.setattr(ts, '_stash_sqlite', lambda *a, **k: False)
    data = {'administrator_password': 'secret', 'hostname': 'H'}
    with app.app_context():
        try:
            ts.stash_task_secrets('fail-closed-1', data)
            assert False, 'expected RuntimeError'
        except RuntimeError as e:
            assert 'stash' in str(e).lower()
        # Secrets restored onto data so caller can clean up; not left only on Redis.
        assert data.get('administrator_password') == 'secret'


def test_load_falls_back_to_sqlite_when_redis_recovers_empty(app, monkeypatch):
    """Redis down at stash → SQLite holds secrets; Redis up at load must still restore."""
    from app import task_secrets as ts

    monkeypatch.setattr(ts, '_stash_redis', lambda *a, **k: False)
    data = {'administrator_password': 'AdminSecret!', 'hostname': 'H'}
    with app.app_context():
        ts.stash_task_secrets('redis-recover-1', data)
        assert 'administrator_password' not in data

        # Healthy Redis with no key looks like a miss (None), not {}.
        monkeypatch.setattr(ts, '_load_redis', lambda *_a, **_k: None)
        restored = {'hostname': 'H'}
        ts.load_task_secrets('redis-recover-1', restored)
        assert restored['administrator_password'] == 'AdminSecret!'
