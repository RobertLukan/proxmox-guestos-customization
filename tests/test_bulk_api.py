"""Bulk provisioning API tests."""
from __future__ import annotations

import pytest

from app import db
from app.models import BatchRequest, Task

pytestmark = pytest.mark.api


def _auth():
    return {'Authorization': 'Bearer tok'}


def _bulk_payload():
    return {
        'request_id': 'bulk-req-1',
        'shared': {
            'template_vmid': 120,
            'cores': 2,
            'ram': 4096,
            'bridge': 'vmbr0',
            'network_mode': 'dhcp',
            'administrator_password': 'password123',
            'timezone': 'UTC',
            'join_domain': False,
            'hostname': 'SHARED-NOT-USED',
        },
        'items': [
            {'hostname': 'VDI-001'},
            {'hostname': 'VDI-002'},
        ],
    }


def test_bulk_start_requires_auth(client):
    resp = client.post('/start_sysprep_bulk_workflow', json=_bulk_payload())
    assert resp.status_code == 401


def test_bulk_start_enqueues_items_and_creates_batch(client, app, monkeypatch):
    app.config['API_TOKENS'] = frozenset({'tok'})
    app.config['BULK_MAX_ITEMS'] = 50
    app.config['BULK_MAX_CONCURRENT_GLOBAL'] = 500
    app.config['BULK_MAX_CONCURRENT_PER_REMOTE'] = 500
    app.config['BULK_MAX_INFLIGHT_BATCHES'] = 100
    app.config['PROVISION_MAX_PER_DAY'] = 500
    monkeypatch.setattr('app.routes.require_sysprep_template', lambda vmid, **kw: 'win11')
    monkeypatch.setattr(
        'app.routes._admit_resource_and_quota',
        lambda *a, **k: (True, [], 'win11', {}),
    )
    calls = []

    class _Task:
        @staticmethod
        def apply_async(args=None, queue=None, **kwargs):
            calls.append((args, queue))

    monkeypatch.setattr('app.routes.sysprep_workflow_task', _Task)
    resp = client.post('/start_sysprep_bulk_workflow', json=_bulk_payload(), headers=_auth())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['accepted_count'] == 2
    assert body['rejected_count'] == 0
    assert body['idempotent_replay'] is False
    assert len(calls) == 2
    assert all(q == 'clone_queue' for _, q in calls)

    with app.app_context():
        batch = db.session.get(BatchRequest, body['batch_id'])
        assert batch is not None
        assert batch.accepted_items == 2
        assert Task.query.filter(Task.batch_id == batch.id).count() == 2


def test_bulk_idempotency_replay_returns_existing_batch(client, app, monkeypatch):
    app.config['API_TOKENS'] = frozenset({'tok'})
    app.config['PROVISION_MAX_PER_DAY'] = 500
    app.config['BULK_MAX_CONCURRENT_GLOBAL'] = 500
    monkeypatch.setattr('app.routes.require_sysprep_template', lambda vmid, **kw: 'win11')
    monkeypatch.setattr(
        'app.routes._admit_resource_and_quota',
        lambda *a, **k: (True, [], 'win11', {}),
    )
    calls = []

    class _Task:
        @staticmethod
        def apply_async(args=None, queue=None, **kwargs):
            calls.append(args)

    monkeypatch.setattr('app.routes.sysprep_workflow_task', _Task)
    payload = _bulk_payload()
    first = client.post('/start_sysprep_bulk_workflow', json=payload, headers=_auth())
    assert first.status_code == 200
    second = client.post('/start_sysprep_bulk_workflow', json=payload, headers=_auth())
    assert second.status_code == 200
    body = second.get_json()
    assert body['idempotent_replay'] is True
    assert len(calls) == 2  # only first submit enqueues


def test_bulk_join_profile_keeps_empty_dns(client, app, monkeypatch):
    """Bulk + profile credentials must not inject profile DNS when shared DNS is blank."""
    app.config['API_TOKENS'] = frozenset({'tok'})
    app.config['DOMAIN_PROFILES'] = {
        'Lab': {
            'dns_servers': '10.0.0.10',
            'domain_name': 'lab.example.com',
            'domain_username': 'svc@lab.example.com',
            'domain_password': 's3cret!',
            'vlan': 100,
        }
    }
    app.config['BULK_MAX_ITEMS'] = 50
    app.config['BULK_MAX_CONCURRENT_GLOBAL'] = 500
    app.config['BULK_MAX_CONCURRENT_PER_REMOTE'] = 500
    app.config['BULK_MAX_INFLIGHT_BATCHES'] = 100
    app.config['PROVISION_MAX_PER_DAY'] = 500
    monkeypatch.setattr('app.routes.require_sysprep_template', lambda vmid, **kw: 'win11')
    monkeypatch.setattr(
        'app.routes.classify_windows_guest_family',
        lambda vmid, **kw: 'win11',
    )
    monkeypatch.setattr(
        'app.routes._admit_resource_and_quota',
        lambda *a, **k: (True, [], 'win11', {}),
    )
    monkeypatch.setattr(
        'app.domain_preflight.check_domain_join_preflight',
        lambda data, timeout=2.0: None,
    )
    calls = []

    class _Task:
        @staticmethod
        def apply_async(args=None, queue=None, **kwargs):
            calls.append(args)

    monkeypatch.setattr('app.routes.sysprep_workflow_task', _Task)
    payload = {
        'request_id': 'bulk-empty-dns',
        'shared': {
            'template_vmid': 127,
            'cores': 2,
            'ram': 4096,
            'bridge': 'vmbr0',
            'network_mode': 'dhcp',
            'dns_servers': '',
            'administrator_password': 'password123',
            'timezone': 'UTC',
            'join_domain': True,
            'use_domain_profile_credentials': True,
            'domain_profile': 'Lab',
        },
        'items': [{'hostname': 'VDI-DNS1'}],
    }
    resp = client.post('/start_sysprep_bulk_workflow', json=payload, headers=_auth())
    assert resp.status_code == 200, resp.get_json()
    assert len(calls) == 1
    task_payload = calls[0][1]
    assert task_payload.get('dns_servers') in ('', None)
    assert task_payload.get('vlan') in ('', None)
    assert task_payload.get('domain_name') == 'lab.example.com'
    # Password may be stashed/scrubbed before enqueue; username should remain.
    assert task_payload.get('domain_username') == 'svc@lab.example.com'
    assert task_payload.get('domain_profile') == 'Lab'


def test_bulk_cancel_marks_tasks_cancelled(client, app):
    app.config['API_TOKENS'] = frozenset({'tok'})
    with app.app_context():
        batch = BatchRequest(id='b-1', request_id='rq-1', status='RUNNING', total_items=2, accepted_items=2)
        db.session.add(batch)
        db.session.add(Task(id='t-b1-1', name='Sysprep Workflow', description='x', batch_id='b-1', status='PENDING'))
        db.session.add(Task(id='t-b1-2', name='Sysprep Workflow', description='x', batch_id='b-1', status='PROGRESS'))
        db.session.commit()
    resp = client.post('/api/batches/b-1/cancel', headers={'X-Api-Token': 'tok'})
    assert resp.status_code == 200
    with app.app_context():
        assert db.session.get(BatchRequest, 'b-1').status == 'CANCELLED'
        assert db.session.get(Task, 't-b1-1').status == 'CANCELLED'
        assert db.session.get(Task, 't-b1-2').status == 'CANCELLED'


def test_task_list_filters_batch_and_cursor(client, app):
    app.config['API_TOKENS'] = frozenset({'tok'})
    with app.app_context():
        db.session.add(Task(id='a1', name='Sysprep Workflow', description='d', batch_id='B1', status='SUCCESS'))
        db.session.add(Task(id='a2', name='Sysprep Workflow', description='d', batch_id='B1', status='SUCCESS'))
        db.session.add(Task(id='a3', name='Sysprep Workflow', description='d', batch_id='B2', status='SUCCESS'))
        db.session.commit()
    r1 = client.get('/api/tasks?batch_id=B1&limit=1', headers={'X-Api-Token': 'tok'})
    assert r1.status_code == 200
    body1 = r1.get_json()
    assert body1['count'] == 1
    assert body1['tasks'][0]['batch_id'] == 'B1'
    cursor = body1.get('next_cursor')
    assert cursor
    r2 = client.get(f'/api/tasks?batch_id=B1&limit=10&cursor={cursor}', headers={'X-Api-Token': 'tok'})
    assert r2.status_code == 200
    body2 = r2.get_json()
    for row in body2['tasks']:
        assert row['batch_id'] == 'B1'


def test_metrics_endpoint(client, app):
    app.config['API_TOKENS'] = frozenset({'tok'})
    with app.app_context():
        db.session.add(BatchRequest(id='b-m', request_id='rq-m', status='RUNNING', total_items=1, accepted_items=1))
        db.session.add(Task(id='m1', name='Sysprep Workflow', description='d', status='PROGRESS', remote_id='r1'))
        db.session.commit()
    resp = client.get('/api/metrics', headers={'X-Api-Token': 'tok'})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['tasks']['inflight'] >= 1
    assert body['batches']['running'] >= 1

