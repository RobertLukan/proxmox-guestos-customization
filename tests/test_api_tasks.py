"""Tests for GET /api/tasks history API."""

from app.models import Task, db


def _seed_tasks(app):
    with app.app_context():
        db.session.add(Task(
            id='t-sys-1',
            name='Sysprep Workflow',
            description='d',
            status='SUCCESS',
            progress=100,
            hostname='HOST1',
            remote_id='vie-1',
            template_vmid=120,
            result_vmid=200,
        ))
        db.session.add(Task(
            id='t-sys-2',
            name='Sysprep Workflow',
            description='d',
            status='PROGRESS',
            progress=40,
            hostname='HOST2',
            remote_id='vie-1',
            template_vmid=122,
        ))
        db.session.add(Task(
            id='t-other',
            name='Power On VM',
            description='d',
            status='SUCCESS',
            progress=100,
            remote_id='vie-1',
        ))
        db.session.add(Task(
            id='t-other-remote',
            name='Sysprep Workflow',
            description='d',
            status='FAILURE',
            progress=100,
            hostname='X',
            remote_id='idr-1',
            template_vmid=50,
        ))
        db.session.commit()


def test_api_tasks_requires_auth(client):
    resp = client.get('/api/tasks')
    assert resp.status_code == 401


def test_api_tasks_lists_customizations(client, app):
    app.config['API_TOKENS'] = frozenset({'tok'})
    _seed_tasks(app)
    resp = client.get('/api/tasks', headers={'Authorization': 'Bearer tok'})
    assert resp.status_code == 200
    body = resp.get_json()
    ids = [t['id'] for t in body['tasks']]
    assert 't-sys-1' in ids
    assert 't-sys-2' in ids
    assert 't-other-remote' in ids
    assert 't-other' not in ids  # kind=customization default


def test_api_tasks_filter_remote_and_running(client, app):
    app.config['API_TOKENS'] = frozenset({'tok'})
    _seed_tasks(app)
    resp = client.get(
        '/api/tasks?remote_id=vie-1&running=1',
        headers={'X-Api-Token': 'tok'},
    )
    assert resp.status_code == 200
    tasks = resp.get_json()['tasks']
    assert len(tasks) == 1
    assert tasks[0]['id'] == 't-sys-2'
    assert tasks[0]['hostname'] == 'HOST2'


def test_api_task_detail(client, app):
    app.config['API_TOKENS'] = frozenset({'tok'})
    _seed_tasks(app)
    resp = client.get('/api/tasks/t-sys-1', headers={'Authorization': 'Bearer tok'})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['result_vmid'] == 200
    assert body['template_vmid'] == 120
    assert body['remote_id'] == 'vie-1'
