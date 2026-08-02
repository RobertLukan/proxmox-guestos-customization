"""API token auth, CSRF exemption, and remote_id resolution for PDM integration."""
import pytest

pytestmark = pytest.mark.api


def test_api_health_and_version(client):
    health = client.get('/api/health').get_json()
    assert health['status'] == 'ok'
    assert 'checks' in health
    assert health['checks'].get('database') == 'ok'
    ver = client.get('/api/version').get_json()
    assert 'version' in ver
    assert ver['version'] == client.application.config['APP_VERSION']
    assert ver.get('min_pdm_guestos') == '2.3.0'
    assert 'build_time' in ver
    assert ver['build_time'] == (client.application.config.get('APP_BUILD_TIME') or None)


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
        def apply_async(args=None, queue=None, **kwargs):
            task_id, data = args
            captured['task_id'] = task_id
            captured['data'] = data

    monkeypatch.setattr('app.routes.sysprep_workflow_task', _Task)
    monkeypatch.setattr('app.routes.require_sysprep_template', lambda vmid, **kw: 'win11')
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


def test_sysprep_start_defaults_bridge_from_primary(client, app, monkeypatch):
    """API clients (PDM smoke) may omit bridge; use PRIMARY_BRIDGE."""
    app.config['API_TOKENS'] = frozenset({'good-token'})
    app.config['PRIMARY_BRIDGE'] = 'vmbr9'
    captured = {}

    class _Task:
        @staticmethod
        def apply_async(args=None, queue=None, **kwargs):
            task_id, data = args
            captured['data'] = data

    monkeypatch.setattr('app.routes.sysprep_workflow_task', _Task)
    monkeypatch.setattr('app.routes.require_sysprep_template', lambda vmid, **kw: 'win11')
    resp = client.post(
        '/start_sysprep_workflow',
        json={
            'hostname': 'BRIDGEDEF01',
            'template_vmid': '100',
            'cores': 2,
            'ram': 4096,
            'network_mode': 'dhcp',
            'administrator_password': 'password123',
            'timezone': 'UTC',
            'join_domain': False,
        },
        headers={'Authorization': 'Bearer good-token'},
    )
    assert resp.status_code == 200
    assert captured['data']['bridge'] == 'vmbr9'


def test_sysprep_start_session_still_needs_csrf(client, monkeypatch):
    class _Task:
        @staticmethod
        def apply_async(*args, **kwargs):
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
        def apply_async(*args, **kwargs):
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


def test_sysprep_workflow_known_remote_id_does_not_put_secrets_in_celery(client, app, monkeypatch):
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
        def apply_async(args=None, queue=None, **kwargs):
            task_id, data = args
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
    assert '_pve' not in captured['data']


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
    from app.remotes import resolve_pve_remote, attach_pve_override
    from app.routes import _json_field_error

    app.config['PVE_REMOTES'] = {
        'lab': {'host': 'h', 'user': 'u', 'password': 'p', 'verify_ssl': True},
    }
    data = {'remote_id': 'lab'}
    with app.app_context():
        ok, err = resolve_pve_remote(data, _json_field_error)
    assert ok and err is None
    assert '_pve' not in data
    assert data['remote_id'] == 'lab'
    with app.app_context():
        override = attach_pve_override(data)
    assert override['host'] == 'h'
    assert override['verify_ssl'] is True
    assert override['password'] == 'p'

    data2 = {'remote_id': 'nope'}
    with app.app_context():
        ok, err = resolve_pve_remote(data2, _json_field_error)
    assert not ok
    assert err[1] == 400
