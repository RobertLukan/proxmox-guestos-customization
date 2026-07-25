"""API token auth, CSRF exemption, and remote_id resolution for PDM integration."""
import pytest

pytestmark = pytest.mark.api


def test_api_health_and_version(client):
    assert client.get('/api/health').get_json() == {'status': 'ok'}
    ver = client.get('/api/version').get_json()
    assert 'version' in ver
    assert ver['version'] == client.application.config['APP_VERSION']


def test_sysprep_start_requires_auth(client):
    resp = client.post('/start_sysprep_workflow', json={'hostname': 'X'})
    assert resp.status_code == 401


def test_sysprep_start_rejects_bad_api_token(client, app):
    app.config['API_TOKENS'] = frozenset({'good-token'})
    resp = client.post(
        '/start_sysprep_workflow',
        json={'hostname': 'X'},
        headers={'Authorization': 'Bearer wrong'},
    )
    assert resp.status_code == 401


def test_sysprep_start_with_api_token_skips_csrf(client, app, monkeypatch):
    app.config['API_TOKENS'] = frozenset({'good-token'})
    captured = {}

    class _Task:
        @staticmethod
        def delay(task_id, data):
            captured['task_id'] = task_id
            captured['data'] = data

    monkeypatch.setattr('app.routes.sysprep_workflow_task', _Task)
    monkeypatch.setattr('app.routes.require_windows_guest', lambda vmid, **kw: 'win11')
    resp = client.post(
        '/start_sysprep_workflow',
        json={
            'hostname': 'APITEST01',
            'template_vmid': '100',
            'cores': 2,
            'ram': 4096,
            'bridge': 'vmbr0',
            'network_mode': 'dhcp',
            'administrator_password': 'password123',
            'timezone': 'UTC',
            'join_domain': False,
        },
        headers={'Authorization': 'Bearer good-token'},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert 'task_id' in body
    assert captured['data']['hostname'] == 'APITEST01'
    assert '_pve' not in captured['data']


def test_sysprep_start_session_still_needs_csrf(client, monkeypatch):
    class _Task:
        @staticmethod
        def delay(*args, **kwargs):
            return None

    monkeypatch.setattr('app.routes.sysprep_workflow_task', _Task)

    # Login without going through CSRF helper for the JSON post
    from tests.test_web import _csrf_token, _login
    _login(client)
    resp = client.post(
        '/start_sysprep_workflow',
        json={'hostname': 'X', 'join_domain': False},
    )
    assert resp.status_code == 400
    assert 'CSRF' in (resp.get_json() or {}).get('error', '')


def test_sysprep_unknown_remote_id(client, app, monkeypatch):
    app.config['API_TOKENS'] = frozenset({'good-token'})
    app.config['PVE_REMOTES'] = {
        'lab': {
            'host': 'pve.lab',
            'user': 'api@pve',
            'password': 'secret',
            'verify_ssl': False,
        }
    }

    class _Task:
        @staticmethod
        def delay(*args, **kwargs):
            raise AssertionError('should not enqueue')

    monkeypatch.setattr('app.routes.sysprep_workflow_task', _Task)
    resp = client.post(
        '/start_sysprep_workflow',
        json={'hostname': 'X', 'join_domain': False, 'remote_id': 'missing'},
        headers={'X-Api-Token': 'good-token'},
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert 'remote_id' in body.get('errors', {})


def test_sysprep_existing_api_disabled(client, app):
    app.config['API_TOKENS'] = frozenset({'good-token'})
    resp = client.post(
        '/start_sysprep_existing_vm_task',
        json={
            'vmid': 121,
            'hostname': 'EXIST01',
            'network_mode': 'dhcp',
            'administrator_password': 'password123',
            'timezone': 'UTC',
            'join_domain': False,
            'remote_id': 'lab',
        },
        headers={'Authorization': 'Bearer good-token'},
    )
    assert resp.status_code == 403
    assert 'disabled' in resp.get_json().get('error', '').lower()


def test_sysprep_workflow_known_remote_id_attaches_pve(client, app, monkeypatch):
    app.config['API_TOKENS'] = frozenset({'good-token'})
    app.config['PVE_REMOTES'] = {
        'lab': {
            'host': 'pve.lab',
            'user': 'api@pve',
            'password': 'secret',
            'verify_ssl': False,
        }
    }
    captured = {}

    class _Task:
        @staticmethod
        def delay(task_id, data):
            captured['data'] = data

    monkeypatch.setattr('app.routes.sysprep_workflow_task', _Task)
    monkeypatch.setattr('app.routes.require_sysprep_template', lambda vmid, **kw: 'win10')
    resp = client.post(
        '/start_sysprep_workflow',
        json={
            'template_vmid': 120,
            'hostname': 'CLONE01',
            'cores': 2,
            'ram': 4096,
            'network_mode': 'dhcp',
            'administrator_password': 'password123',
            'timezone': 'UTC',
            'join_domain': False,
            'remote_id': 'lab',
        },
        headers={'Authorization': 'Bearer good-token'},
    )
    assert resp.status_code == 200
    assert captured['data']['remote_id'] == 'lab'
    assert captured['data']['_pve']['host'] == 'pve.lab'
    assert captured['data']['_pve']['user'] == 'api@pve'
    assert captured['data']['_pve']['password'] == 'secret'


def test_task_status_allows_api_token(client, app):
    from app.models import Task
    from app import db

    app.config['API_TOKENS'] = frozenset({'good-token'})
    with app.app_context():
        task = Task(id='task-api-1', name='Sysprep Existing VM', description='t')
        db.session.add(task)
        db.session.commit()

    resp = client.get('/task_status/task-api-1', headers={'X-Api-Token': 'good-token'})
    assert resp.status_code == 200
    assert resp.get_json()['id'] == 'task-api-1'


def test_resolve_pve_remote_helper(app):
    from app.remotes import resolve_pve_remote
    from app.routes import _json_field_error

    app.config['PVE_REMOTES'] = {
        'lab': {'host': 'h', 'user': 'u', 'password': 'p', 'verify_ssl': True},
    }
    data = {'remote_id': 'lab'}
    with app.app_context():
        ok, err = resolve_pve_remote(data, _json_field_error)
    assert ok and err is None
    assert data['_pve']['host'] == 'h'
    assert data['_pve']['verify_ssl'] is True

    data2 = {'remote_id': 'nope'}
    with app.app_context():
        ok, err = resolve_pve_remote(data2, _json_field_error)
    assert not ok
    assert err[1] == 400
