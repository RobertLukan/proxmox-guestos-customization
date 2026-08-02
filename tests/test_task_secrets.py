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
