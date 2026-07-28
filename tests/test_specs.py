"""Customization Spec CRUD and apply helpers."""
from __future__ import annotations

import re

from app.models import CustomizationSpec
from app.specs import resolve_spec_from_request, sanitize_spec_payload


def _csrf_token(html):
    match = re.search(rb'name="csrf_token" value="([^"]+)"', html)
    if match:
        return match.group(1).decode()
    match = re.search(rb'name="csrf-token" content="([^"]+)"', html)
    assert match, 'csrf token not found'
    return match.group(1).decode()


def _login(client):
    token = _csrf_token(client.get('/login').data)
    resp = client.post('/login', data={'password': 'changeme', 'csrf_token': token})
    assert resp.status_code == 302


def test_sanitize_drops_secrets_and_unknown():
    clean = sanitize_spec_payload({
        'timezone': 'UTC',
        'administrator_password': 'secret',
        'domain_password': 'secret',
        'unknown_field': 1,
        'locale': 'de-DE',
    })
    assert clean == {'timezone': 'UTC', 'locale': 'de-DE'}


def test_resolve_spec_merges_without_overwriting_password(app):
    with app.app_context():
        s = CustomizationSpec(name='LabStatic', description='test')
        s.set_payload({
            'timezone': 'UTC',
            'locale': 'en-GB',
            'network_mode': 'dhcp',
            'administrator_password': 'ignored',
        })
        from app import db
        db.session.add(s)
        db.session.commit()

        payload = {
            'hostname': 'HOST1',
            'administrator_password': 'OperatorSet123!',
            'spec_name': 'LabStatic',
        }
        ok, err = resolve_spec_from_request(payload)
        assert ok and err is None
        assert payload['timezone'] == 'UTC'
        assert payload['locale'] == 'en-GB'
        assert payload['network_mode'] == 'dhcp'
        assert payload['administrator_password'] == 'OperatorSet123!'
        assert payload['applied_spec_name'] == 'LabStatic'


def test_spec_api_crud(client, app):
    app.config['API_TOKENS'] = frozenset({'spec-token'})
    headers = {'Authorization': 'Bearer spec-token'}
    create = client.post('/api/specs', json={
        'name': 'ApiSpec',
        'description': 'dhcp defaults',
        'payload': {
            'timezone': 'UTC',
            'locale': 'en-GB',
            'network_mode': 'dhcp',
            'administrator_password': 'should-not-store',
        },
    }, headers=headers)
    assert create.status_code == 201, create.data
    body = create.get_json()
    assert body['name'] == 'ApiSpec'
    assert 'administrator_password' not in body['payload']
    assert body['payload']['timezone'] == 'UTC'

    listed = client.get('/api/specs', headers=headers)
    assert listed.status_code == 200
    assert any(s['name'] == 'ApiSpec' for s in listed.get_json()['specs'])

    _login(client)
    page = client.get('/specs')
    assert page.status_code == 200
    assert b'ApiSpec' in page.data

    deleted = client.delete(f"/api/specs/{body['id']}", headers=headers)
    assert deleted.status_code == 200, deleted.data
