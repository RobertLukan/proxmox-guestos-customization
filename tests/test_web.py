import re

import pytest

pytestmark = pytest.mark.web


def _csrf_token(html):
    match = re.search(rb'name="csrf_token" value="([^"]+)"', html)
    assert match, 'csrf_token field not found'
    return match.group(1).decode()


def test_login_page_renders_with_csrf(client):
    resp = client.get('/login')
    assert resp.status_code == 200
    assert b'name="csrf_token"' in resp.data


def test_state_changing_post_without_csrf_is_rejected(client):
    resp = client.post('/select', data={'template_vmid': 1})
    assert resp.status_code == 400  # CSRF rejection happens before the view


def test_protected_route_redirects_when_anonymous(client):
    resp = client.get('/')
    assert resp.status_code == 302
    assert '/login' in resp.headers['Location']


def test_login_flow_and_change_password(client):
    token = _csrf_token(client.get('/login').data)
    resp = client.post('/login', data={'password': 'changeme', 'csrf_token': token})
    assert resp.status_code == 302  # successful login redirects to index

    # Now authenticated: the home page loads.
    assert client.get('/').status_code == 200

    # Change the password.
    token = _csrf_token(client.get('/change_password').data)
    resp = client.post('/change_password', data={
        'current_password': 'changeme',
        'new_password': 'a-better-password',
        'confirm_password': 'a-better-password',
        'csrf_token': token,
    })
    assert resp.status_code == 302  # success redirects to index

    # Old password no longer works after logout.
    client.get('/logout')
    token = _csrf_token(client.get('/login').data)
    resp = client.post('/login', data={'password': 'changeme', 'csrf_token': token})
    assert resp.status_code == 200  # re-rendered login page with a flash, not a redirect


def test_change_password_rejects_default_password(client):
    _login(client)
    token = _csrf_token(client.get('/change_password').data)
    resp = client.post('/change_password', data={
        'current_password': 'changeme',
        'new_password': 'changeme',
        'confirm_password': 'changeme',
        'csrf_token': token,
    })
    assert resp.status_code == 200
    assert b'shipped default' in resp.data


def test_default_password_gate_blocks_ui_when_not_testing(client, app):
    """Outside TESTING, session users must change changeme before the home page."""
    app.config['TESTING'] = False
    try:
        token = _csrf_token(client.get('/login').data)
        resp = client.post('/login', data={'password': 'changeme', 'csrf_token': token})
        assert resp.status_code == 302
        assert '/change_password' in resp.headers['Location']
        blocked = client.get('/')
        assert blocked.status_code == 302
        assert '/change_password' in blocked.headers['Location']
    finally:
        app.config['TESTING'] = True


def test_change_password_rejects_wrong_current(client):
    token = _csrf_token(client.get('/login').data)
    client.post('/login', data={'password': 'changeme', 'csrf_token': token})

    token = _csrf_token(client.get('/change_password').data)
    resp = client.post('/change_password', data={
        'current_password': 'wrong',
        'new_password': 'a-better-password',
        'confirm_password': 'a-better-password',
        'csrf_token': token,
    })
    assert resp.status_code == 200  # stays on the form
    assert b'Current password is incorrect' in resp.data


def _login(client):
    token = _csrf_token(client.get('/login').data)
    client.post('/login', data={'password': 'changeme', 'csrf_token': token})


def test_index_uses_base_layout_with_csrf_meta(client, monkeypatch):
    monkeypatch.setattr(
        'app.routes.get_template_vms',
        lambda: [{'vmid': 100, 'name': 'Win11-Template'}],
    )
    _login(client)
    resp = client.get('/')
    assert resp.status_code == 200
    assert b'csrf-token' in resp.data
    assert b'js/csrf.js' in resp.data
    assert b'css/app.css' in resp.data
    assert b'Proxmox GuestOS' in resp.data
    assert b'app-version' in resp.data
    assert b'v' + client.application.config['APP_VERSION'].encode() in resp.data
    assert b'Clone + Sysprep (customize)' in resp.data
    assert b'WinRM' not in resp.data
    assert b'Windows template' in resp.data
    assert b'In-place Sysprep' in resp.data or b'not allowed' in resp.data


def test_select_template_sysprep_later_redirects_to_sysprep_form(client, monkeypatch):
    monkeypatch.setattr('app.routes.require_sysprep_template', lambda vmid, **kw: 'win11')
    monkeypatch.setattr(
        'app.routes.get_network_bridges',
        lambda: [{'iface': 'vmbr0'}],
    )
    monkeypatch.setattr(
        'app.routes.get_template_vms',
        lambda: [{'vmid': 100, 'name': 'Win11-Template'}],
    )
    _login(client)
    token = _csrf_token(client.get('/').data)
    resp = client.post('/select', data={
        'template_vmid': '100',
        'purpose': 'sysprep_later',
        'csrf_token': token,
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b'name="template_vmid"' in resp.data
    assert b'value="100"' in resp.data
    assert b'data-wizard' in resp.data


def test_select_template_rejects_non_sysprep_template(client, monkeypatch):
    monkeypatch.setattr(
        'app.routes.get_template_vms',
        lambda: [{'vmid': 100, 'name': 'Win11-Template'}],
    )
    def _reject(vmid, **_kw):
        raise ValueError(f'VM {vmid} is not a Windows guest (ostype="l26").')
    monkeypatch.setattr('app.routes.require_sysprep_template', _reject)
    _login(client)
    token = _csrf_token(client.get('/').data)
    resp = client.post('/select', data={
        'template_vmid': '50',
        'csrf_token': token,
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b'not a Windows guest' in resp.data


def test_login_shows_app_version(client):
    resp = client.get('/login')
    assert resp.status_code == 200
    assert b'app-version' in resp.data
    assert client.application.config['APP_VERSION'].encode() in resp.data


def test_sysprep_form_wizard_includes_csrf_and_payload_fields(client, monkeypatch):
    monkeypatch.setattr(
        'app.routes.get_network_bridges',
        lambda: [{'iface': 'vmbr0'}],
    )
    monkeypatch.setattr('app.routes.require_sysprep_template', lambda vmid, **kw: 'win11')
    _login(client)
    token = _csrf_token(client.get('/').data)
    resp = client.post('/sysprep_form', data={
        'template_vmid': '100',
        'csrf_token': token,
    })
    assert resp.status_code == 200
    html = resp.data
    assert b'csrf-token' in html
    assert b'data-wizard' in html
    assert b'name="hostname"' in html
    assert b'name="administrator_password"' in html
    assert b'id="administrator_password_confirm"' in html
    assert b'name="network_mode"' in html
    assert b'name="ip_address"' in html
    assert b'name="domain_profile"' in html
    assert b'id="use_domain_profile_network"' in html
    assert b'id="join_domain_checkbox"' in html
    assert b'id="use_domain_profile_credentials"' in html
    assert b'Locks DNS and VLAN to the selected profile' in html
    assert b'js/validate.js' in html
    assert b'js/wizard.js' in html
    assert b'wizard-panel' in html


def test_sysprep_form_preselects_primary_bridge(client, app, monkeypatch):
    app.config['PRIMARY_BRIDGE'] = 'vmbr4'
    monkeypatch.setattr(
        'app.routes.get_network_bridges',
        lambda: [{'iface': 'vmbr0'}, {'iface': 'vmbr4'}, {'iface': 'vmbr8'}],
    )
    monkeypatch.setattr('app.routes.require_sysprep_template', lambda vmid, **kw: 'win11')
    _login(client)
    token = _csrf_token(client.get('/').data)
    resp = client.post('/sysprep_form', data={
        'template_vmid': '100',
        'csrf_token': token,
    })
    assert resp.status_code == 200
    html = resp.data.decode('utf-8')
    assert 'value="vmbr4" selected' in html
    assert 'value="vmbr0" selected' not in html


def test_sysprep_existing_form_redirects_non_template(client, monkeypatch):
    monkeypatch.setattr('app.routes.is_proxmox_template', lambda vmid, **kw: False)
    monkeypatch.setattr(
        'app.routes.get_template_vms',
        lambda: [{'vmid': 100, 'name': 'Win11-Template'}],
    )
    _login(client)
    resp = client.get('/sysprep_existing_vm_form/121', follow_redirects=True)
    assert resp.status_code == 200
    assert b'disabled' in resp.data.lower() or b'not allowed' in resp.data.lower() or b'protect' in resp.data.lower()


def test_sysprep_existing_form_template_redirects_to_sysprep_form(client, monkeypatch):
    monkeypatch.setattr('app.routes.is_proxmox_template', lambda vmid, **kw: True)
    monkeypatch.setattr('app.routes.require_sysprep_template', lambda vmid, **kw: 'win10')
    monkeypatch.setattr(
        'app.routes.get_network_bridges',
        lambda: [{'iface': 'vmbr0'}],
    )
    _login(client)
    resp = client.get('/sysprep_existing_vm_form/120?remote_id=lab', follow_redirects=True)
    assert resp.status_code == 200
    assert b'name="template_vmid"' in resp.data
    assert b'value="120"' in resp.data
    assert b'value="lab"' in resp.data


def test_sysprep_form_get_deep_link(client, monkeypatch):
    monkeypatch.setattr(
        'app.routes.get_network_bridges',
        lambda: [{'iface': 'vmbr0'}],
    )
    monkeypatch.setattr('app.routes.require_sysprep_template', lambda vmid, **kw: 'win11')
    _login(client)
    resp = client.get('/sysprep_form?template_vmid=100&remote_id=lab')
    assert resp.status_code == 200
    assert b'name="template_vmid"' in resp.data
    assert b'value="100"' in resp.data
    assert b'name="remote_id"' in resp.data
    assert b'value="lab"' in resp.data

def test_start_sysprep_unknown_profile_returns_field_errors(client, monkeypatch):
    class _Task:
        @staticmethod
        def delay(*args, **kwargs):
            return None

    monkeypatch.setattr('app.routes.sysprep_workflow_task', _Task)
    _login(client)
    # CSRF for JSON: Flask-WTF expects header from csrf.js; use cookie/token pattern
    token = _csrf_token(client.get('/').data)
    resp = client.post(
        '/start_sysprep_workflow',
        json={
            'hostname': 'TESTHOST',
            'join_domain': True,
            'use_domain_profile_credentials': True,
            'domain_profile': 'does-not-exist',
        },
        headers={'X-CSRFToken': token},
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert 'error' in body
    assert body.get('errors', {}).get('domain_profile')
