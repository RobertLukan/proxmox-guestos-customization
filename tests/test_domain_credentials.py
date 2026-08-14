"""Tests for domain credential normalization and failure formatting."""
import pytest

from app.domain_credentials import (
    classify_ldap_error,
    format_cred_probe_failure,
    normalize_domain_password,
    normalize_domain_username,
    prepare_join_credentials,
)
from app.domain_guest_probe import _parse_probe_line
from app.validators import ValidationError


def test_normalize_username_upn():
    assert normalize_domain_username('  svc@lab.test ') == 'svc@lab.test'


def test_normalize_username_downlevel():
    assert normalize_domain_username(r'LAB\svc-join') == r'LAB\svc-join'
    assert normalize_domain_username('LAB/svc-join') == r'LAB\svc-join'


def test_normalize_username_rejects_bare():
    with pytest.raises(ValidationError, match='bare names'):
        normalize_domain_username('svc-join')


def test_normalize_username_strips_zwsp():
    assert normalize_domain_username('svc\u200b@lab.test') == 'svc@lab.test'


def test_normalize_password_trims_trailing_newlines_only():
    pw, trimmed = normalize_domain_password('secret\r\n')
    assert pw == 'secret'
    assert trimmed is True
    pw2, trimmed2 = normalize_domain_password('  spaced  ')
    assert pw2 == '  spaced  '
    assert trimmed2 is False


def test_prepare_join_credentials_packs_normalized():
    data = {
        'domain_name': 'Lab.Test',
        'domain_username': 'svc@lab.test',
        'domain_password': 'p@ss\n',
        'domain_ou': ' OU=VMs,DC=lab,DC=test ',
    }
    blob = prepare_join_credentials(data)
    assert blob['domain'] == 'lab.test'
    assert blob['username'] == 'svc@lab.test'
    assert blob['password'] == 'p@ss'
    assert blob['ou'] == 'OU=VMs,DC=lab,DC=test'
    assert data['domain_password'] == 'p@ss'


def test_format_cred_probe_failure_has_debug_fields_no_password():
    msg = format_cred_probe_failure(
        error_class='invalid_credentials',
        domain='lab.test',
        username='svc@lab.test',
        dns_servers='192.168.123.191',
        bind_target='192.168.123.191:389',
        guest_ip='192.168.123.55',
        result='invalid credentials (0x8007052e)',
        domain_profile='Lab',
    )
    assert 'class=invalid_credentials' in msg
    assert 'username=svc@lab.test' in msg
    assert 'guest_ip=192.168.123.55' in msg
    assert 'domain_profile=Lab' in msg
    assert 'password' not in msg.lower() or 'invalid credentials' in msg.lower()
    assert 'SecretPass' not in msg


def test_classify_ldap_error():
    assert classify_ldap_error('invalidCredentials')[0] == 'invalid_credentials'
    assert classify_ldap_error('account locked out')[0] == 'account_restricted'
    assert classify_ldap_error('connection timed out')[0] == 'unreachable'


def test_parse_probe_line():
    ok = _parse_probe_line('OK|ok|OK|bind_target=10.0.0.1:389|guest_ip=10.0.0.9')
    assert ok['ok'] is True
    assert ok['bind_target'] == '10.0.0.1:389'
    fail = _parse_probe_line(
        'FAIL|invalid_credentials|bad pass|bind_target=x:389|guest_ip=1.2.3.4'
    )
    assert fail['ok'] is False
    assert fail['class'] == 'invalid_credentials'


def test_probe_skips_when_disabled(app, monkeypatch):
    from app.domain_guest_probe import probe_domain_credentials_in_guest

    flask_app = app
    flask_app.config['DOMAIN_JOIN_CRED_PROBE'] = False
    with flask_app.app_context():
        out = probe_domain_credentials_in_guest(
            1, {'join_domain': True, 'domain_name': 'lab.test'}
        )
    assert out.get('skipped') is True


def test_probe_fail_classifies_invalid(app, monkeypatch):
    from app import domain_guest_probe as dgp

    flask_app = app
    flask_app.config['DOMAIN_JOIN_CRED_PROBE'] = True
    flask_app.config['DOMAIN_JOIN_CRED_PROBE_WAIT_SECONDS'] = 30

    monkeypatch.setattr(dgp, 'run_command_in_guest', lambda *a, **k: (
        'FAIL|invalid_credentials|0x8007052e|bind_target=192.168.1.1:389|guest_ip=10.0.0.5'
    ))
    monkeypatch.setattr(dgp, 'write_file_to_guest', lambda *a, **k: None)

    data = {
        'join_domain': True,
        'domain_name': 'lab.test',
        'domain_username': 'svc@lab.test',
        'domain_password': 'wrong',
        'dns_servers': '192.168.1.1',
    }
    with flask_app.app_context():
        with pytest.raises(ValidationError) as ei:
            dgp.probe_domain_credentials_in_guest(42, data)
    msg = str(ei.value)
    assert 'invalid_credentials' in msg
    assert 'svc@lab.test' in msg
    assert 'wrong' not in msg


def test_host_ldap_computer_collision(monkeypatch):
    from app import domain_credentials as dc
    import ldap3

    class FakeConn:
        entries = [object()]

        def search(self, *a, **k):
            return True

        def unbind(self):
            pass

    class FakeServer:
        def __init__(self, *a, **k):
            pass

    monkeypatch.setattr(ldap3, 'Server', FakeServer)
    monkeypatch.setattr(ldap3, 'Connection', lambda *a, **k: FakeConn())

    data = {
        'domain_name': 'lab.test',
        'domain_username': 'svc@lab.test',
        'domain_password': 'x',
        'dns_servers': '10.0.0.1',
    }
    with pytest.raises(ValidationError, match='already exists'):
        dc.host_ldap_check_computer_exists(data, 'duphost')


def test_host_ldap_ou_missing(monkeypatch):
    from app import domain_credentials as dc
    import ldap3

    class FakeConn:
        entries = []

        def search(self, *a, **k):
            return False

        def unbind(self):
            pass

    class FakeServer:
        def __init__(self, *a, **k):
            pass

    monkeypatch.setattr(ldap3, 'Server', FakeServer)
    monkeypatch.setattr(ldap3, 'Connection', lambda *a, **k: FakeConn())

    data = {
        'domain_name': 'lab.test',
        'domain_username': 'svc@lab.test',
        'domain_password': 'x',
        'dns_servers': '10.0.0.1',
        'domain_ou': 'OU=Missing,DC=lab,DC=test',
    }
    with pytest.raises(ValidationError, match='domain_ou'):
        dc.host_ldap_validate_ou(data)


def test_api_domain_test_credentials_manual(client, app, monkeypatch):
    app.config['API_TOKENS'] = frozenset({'tok'})

    def _fake_bind(data, timeout=5.0):
        return {
            'ok': True,
            'class': 'ok',
            'result': 'OK',
            'bind_target': '10.0.0.1:389',
            'domain': 'lab.test',
            'username': 'svc@lab.test',
        }

    monkeypatch.setattr('app.domain_credentials.host_ldap_bind', _fake_bind)
    resp = client.post(
        '/api/domain/test_credentials',
        json={
            'use_domain_profile_credentials': False,
            'domain_name': 'lab.test',
            'domain_username': 'svc@lab.test',
            'domain_password': 'x',
            'dns_servers': '10.0.0.1',
        },
        headers={'Authorization': 'Bearer tok'},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['ok'] is True
    assert body['username'] == 'svc@lab.test'
    assert 'password' not in body


def test_api_domain_test_credentials_profile(client, app, monkeypatch):
    app.config['API_TOKENS'] = frozenset({'tok'})
    app.config['DOMAIN_PROFILES'] = {
        'Lab': {
            'dns_servers': '10.0.0.1',
            'domain_name': 'lab.test',
            'domain_username': 'svc@lab.test',
            'domain_password': 's3cret!',
        }
    }

    def _fake_bind(data, timeout=5.0):
        assert data['domain_password'] == 's3cret!'
        return {
            'ok': False,
            'class': 'invalid_credentials',
            'result': 'bad',
            'bind_target': '10.0.0.1:389',
            'domain': 'lab.test',
            'username': 'svc@lab.test',
        }

    monkeypatch.setattr('app.domain_credentials.host_ldap_bind', _fake_bind)
    resp = client.post(
        '/api/domain/test_credentials',
        json={'domain_profile': 'Lab', 'use_domain_profile_credentials': True},
        headers={'Authorization': 'Bearer tok'},
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body['ok'] is False
    assert body['class'] == 'invalid_credentials'
    assert 's3cret' not in str(body)
