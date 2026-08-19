"""Tests for domain credential normalization and failure formatting."""
import pytest

from app.domain_credentials import (
    classify_ldap_error,
    escape_user_ldap_dn,
    format_cred_probe_failure,
    host_ldap_check_join_ou,
    is_computers_container_dn,
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


def test_host_ldap_ou_unreachable_is_soft(monkeypatch):
    """GuestOS host cannot reach LDAP — OU check skipped (guest path may work)."""
    from app import domain_credentials as dc
    import ldap3
    from ldap3.core.exceptions import LDAPException

    class Boom:
        def __init__(self, *a, **k):
            raise LDAPException('connection refused')

    monkeypatch.setattr(ldap3, 'Server', Boom)
    monkeypatch.setattr(ldap3, 'Connection', Boom)

    data = {
        'domain_name': 'lab.test',
        'domain_username': 'svc@lab.test',
        'domain_password': 'x',
        'dns_servers': '10.0.0.1',
        'domain_ou': 'OU=Servers,DC=lab,DC=test',
    }
    dc.host_ldap_validate_ou(data)  # does not raise


class _FakeAttr:
    def __init__(self, value):
        self.value = value
        self.values = list(value) if isinstance(value, (list, tuple)) else [value]


class _FakeEntry:
    def __init__(self, dn='', **attrs):
        self.entry_dn = dn
        for key, val in attrs.items():
            setattr(self, key, _FakeAttr(val))


def _patch_ldap_conn(monkeypatch, conn_factory):
    import ldap3

    class FakeServer:
        def __init__(self, *a, **k):
            pass

    monkeypatch.setattr(ldap3, 'Server', FakeServer)
    monkeypatch.setattr(ldap3, 'Connection', lambda *a, **k: conn_factory())


def _sample_join_data(**extra):
    data = {
        'domain_name': 'lab.test',
        'domain_username': 'svc@lab.test',
        'domain_password': 'x',
        'dns_servers': '10.0.0.1',
    }
    data.update(extra)
    return data


def test_is_computers_container_dn():
    assert is_computers_container_dn('CN=Computers,DC=lab,DC=test') is True
    assert is_computers_container_dn('cn=computers,DC=LAB,DC=TEST') is True
    assert is_computers_container_dn('OU=Computers,DC=lab,DC=test') is False
    assert is_computers_container_dn('OU=Servers,DC=lab,DC=test') is False


def test_escape_user_ldap_dn_round_trips():
    assert (
        escape_user_ldap_dn('OU=Servers,DC=lab,DC=test')
        == 'OU=Servers,DC=lab,DC=test'
    )
    assert (
        escape_user_ldap_dn('ou=Workstations,dc=lab,dc=test')
        == 'OU=Workstations,DC=lab,DC=test'
    )


def test_escape_user_ldap_dn_rejects_injection_and_garbage():
    with pytest.raises(Exception):
        escape_user_ldap_dn('OU=foo)(|(cn=*,DC=lab,DC=test')
    with pytest.raises(Exception):
        escape_user_ldap_dn('not a dn')


def test_host_ldap_check_join_ou_invalid_dn_does_not_bind():
    info = host_ldap_check_join_ou(_sample_join_data(domain_ou='not a dn'))
    assert info['ok'] is False
    assert info['class'] == 'invalid_ou'


def test_host_ldap_ou_rejects_computers_container(monkeypatch):
    from app import domain_credentials as dc

    class FakeConn:
        def search(self, *a, **k):
            self.entries = [
                _FakeEntry(
                    dn='CN=Computers,DC=lab,DC=test',
                    objectClass=['top', 'container'],
                    distinguishedName='CN=Computers,DC=lab,DC=test',
                )
            ]
            return True

        def unbind(self):
            pass

    _patch_ldap_conn(monkeypatch, FakeConn)
    with pytest.raises(ValidationError, match='not an OU'):
        dc.host_ldap_validate_ou(
            _sample_join_data(domain_ou='CN=Computers,DC=lab,DC=test')
        )


def test_host_ldap_ou_kill_switch_allows_computers_container(app, monkeypatch):
    from app import domain_credentials as dc

    def _boom(*a, **k):
        raise AssertionError('LDAP must not run when OU validate is off')

    monkeypatch.setattr(dc, 'host_ldap_check_join_ou', _boom)
    app.config['DOMAIN_JOIN_VALIDATE_OU'] = False
    try:
        with app.app_context():
            dc.host_ldap_validate_ou(
                _sample_join_data(domain_ou='CN=Computers,DC=lab,DC=test')
            )
    finally:
        app.config['DOMAIN_JOIN_VALIDATE_OU'] = True


def test_host_ldap_ou_accepts_organizational_unit(monkeypatch):
    from app import domain_credentials as dc

    class FakeConn:
        def search(self, *a, **k):
            self.entries = [
                _FakeEntry(
                    dn='OU=Servers,DC=lab,DC=test',
                    objectClass=['top', 'organizationalUnit'],
                    distinguishedName='OU=Servers,DC=lab,DC=test',
                )
            ]
            return True

        def unbind(self):
            pass

    _patch_ldap_conn(monkeypatch, FakeConn)
    dc.host_ldap_validate_ou(
        _sample_join_data(domain_ou='OU=Servers,DC=lab,DC=test')
    )


def test_host_ldap_check_join_ou_empty_warns_default_container(monkeypatch):
    class FakeConn:
        def search(self, base, filt, search_scope=None, attributes=None, **k):
            attrs = list(attributes or [])
            base = str(base or '')
            if base == '':
                self.entries = [
                    _FakeEntry(dn='', defaultNamingContext='DC=lab,DC=test')
                ]
                return True
            if base.lower() == 'dc=lab,dc=test' and 'wellKnownObjects' in attrs:
                self.entries = [
                    _FakeEntry(
                        dn=base,
                        wellKnownObjects=[
                            'B:32:AA312825768811D1ADED00C04FD8D5CD:'
                            'CN=Computers,DC=lab,DC=test'
                        ],
                    )
                ]
                return True
            if base.lower() == 'cn=computers,dc=lab,dc=test':
                self.entries = [
                    _FakeEntry(
                        dn=base,
                        objectClass=['top', 'container'],
                        distinguishedName=base,
                    )
                ]
                return True
            self.entries = []
            return False

        def unbind(self):
            pass

    _patch_ldap_conn(monkeypatch, FakeConn)
    info = host_ldap_check_join_ou(_sample_join_data(domain_ou=''))
    assert info['ok'] is False
    assert info['class'] == 'empty_default_container'
    assert info['default_computer_container'] == 'CN=Computers,DC=lab,DC=test'
    assert '24H2' in (info.get('warning') or '')


def test_host_ldap_validate_ou_rejects_empty_default_container(monkeypatch):
    from app import domain_credentials as dc

    class FakeConn:
        def search(self, base, filt, search_scope=None, attributes=None, **k):
            attrs = list(attributes or [])
            base = str(base or '')
            if base == '':
                self.entries = [
                    _FakeEntry(dn='', defaultNamingContext='DC=lab,DC=test')
                ]
                return True
            if base.lower() == 'dc=lab,dc=test' and 'wellKnownObjects' in attrs:
                self.entries = [
                    _FakeEntry(
                        dn=base,
                        wellKnownObjects=[
                            'B:32:AA312825768811D1ADED00C04FD8D5CD:'
                            'CN=Computers,DC=lab,DC=test'
                        ],
                    )
                ]
                return True
            if base.lower() == 'cn=computers,dc=lab,dc=test':
                self.entries = [
                    _FakeEntry(
                        dn=base,
                        objectClass=['top', 'container'],
                        distinguishedName=base,
                    )
                ]
                return True
            self.entries = []
            return False

        def unbind(self):
            pass

    _patch_ldap_conn(monkeypatch, FakeConn)
    with pytest.raises(ValidationError, match='Target OU is empty'):
        dc.host_ldap_validate_ou(_sample_join_data(domain_ou=''))


def test_host_ldap_validate_ou_empty_ok_when_redircmp(monkeypatch):
    from app import domain_credentials as dc

    redirected = 'OU=Workstations,DC=lab,DC=test'

    class FakeConn:
        def search(self, base, filt, search_scope=None, attributes=None, **k):
            attrs = list(attributes or [])
            base = str(base or '')
            if base == '':
                self.entries = [
                    _FakeEntry(dn='', defaultNamingContext='DC=lab,DC=test')
                ]
                return True
            if base.lower() == 'dc=lab,dc=test' and 'wellKnownObjects' in attrs:
                self.entries = [
                    _FakeEntry(
                        dn=base,
                        wellKnownObjects=[
                            f'B:32:AA312825768811D1ADED00C04FD8D5CD:{redirected}'
                        ],
                    )
                ]
                return True
            if base == redirected:
                self.entries = [
                    _FakeEntry(
                        dn=base,
                        objectClass=['top', 'organizationalUnit'],
                        distinguishedName=base,
                    )
                ]
                return True
            self.entries = []
            return False

        def unbind(self):
            pass

    _patch_ldap_conn(monkeypatch, FakeConn)
    dc.host_ldap_validate_ou(_sample_join_data(domain_ou=''))


def test_host_ldap_check_join_ou_empty_ok_when_redircmp(monkeypatch):
    redirected = 'OU=Workstations,DC=lab,DC=test'

    class FakeConn:
        def search(self, base, filt, search_scope=None, attributes=None, **k):
            attrs = list(attributes or [])
            base = str(base or '')
            if base == '':
                self.entries = [
                    _FakeEntry(dn='', defaultNamingContext='DC=lab,DC=test')
                ]
                return True
            if base.lower() == 'dc=lab,dc=test' and 'wellKnownObjects' in attrs:
                self.entries = [
                    _FakeEntry(
                        dn=base,
                        wellKnownObjects=[
                            f'B:32:AA312825768811D1ADED00C04FD8D5CD:{redirected}'
                        ],
                    )
                ]
                return True
            if base == redirected:
                self.entries = [
                    _FakeEntry(
                        dn=base,
                        objectClass=['top', 'organizationalUnit'],
                        distinguishedName=base,
                    )
                ]
                return True
            self.entries = []
            return False

        def unbind(self):
            pass

    _patch_ldap_conn(monkeypatch, FakeConn)
    info = host_ldap_check_join_ou(_sample_join_data(domain_ou=''))
    assert info['ok'] is True
    assert info.get('warning') is None
    assert info['default_computer_container'] == redirected


def _ok_ou_info(data, timeout=5.0):
    ou = (data.get('domain_ou') or '').strip() or None
    return {
        'ok': True,
        'class': 'ok',
        'result': 'OK',
        'skipped': False,
        'domain_ou': ou,
        'default_computer_container': None,
        'warning': None,
    }


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
    monkeypatch.setattr('app.domain_credentials.host_ldap_check_join_ou', _ok_ou_info)
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
        assert data['dns_servers'] == '10.0.0.1'
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
        json={
            'domain_profile': 'Lab',
            'use_domain_profile_credentials': True,
            'dns_servers': '203.0.113.8',
        },
        headers={'Authorization': 'Bearer tok'},
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body['ok'] is False
    assert body['class'] == 'invalid_credentials'
    assert body.get('continue_allowed') is True
    assert 'GuestOS' in (body.get('advisory') or '')
    assert 's3cret' not in str(body)
    assert body.get('dns_servers') == '10.0.0.1'


def test_api_domain_test_credentials_rejects_computers_container(client, app, monkeypatch):
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

    def _fake_ou(data, timeout=5.0):
        return {
            'ok': False,
            'class': 'not_an_ou',
            'result': (
                "domain_ou 'CN=Computers,DC=lab,DC=test' is the default "
                'Computers container, not an OU.'
            ),
            'skipped': False,
            'domain_ou': 'CN=Computers,DC=lab,DC=test',
            'default_computer_container': None,
            'warning': 'not an OU',
        }

    monkeypatch.setattr('app.domain_credentials.host_ldap_bind', _fake_bind)
    monkeypatch.setattr('app.domain_credentials.host_ldap_check_join_ou', _fake_ou)
    resp = client.post(
        '/api/domain/test_credentials',
        json={
            'use_domain_profile_credentials': False,
            'domain_name': 'lab.test',
            'domain_username': 'svc@lab.test',
            'domain_password': 'x',
            'dns_servers': '10.0.0.1',
            'domain_ou': 'CN=Computers,DC=lab,DC=test',
        },
        headers={'Authorization': 'Bearer tok'},
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body['ok'] is False
    assert body['class'] == 'not_an_ou'
    assert body.get('continue_allowed') is True
    assert 'organizationalUnit' in (body.get('advisory') or '')
    assert body.get('domain_ou') == 'CN=Computers,DC=lab,DC=test'


def test_api_domain_test_credentials_empty_ou_warns(client, app, monkeypatch):
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

    def _fake_ou(data, timeout=5.0):
        assert not (data.get('domain_ou') or '').strip()
        return {
            'ok': False,
            'class': 'empty_default_container',
            'result': 'warning',
            'skipped': False,
            'domain_ou': None,
            'default_computer_container': 'CN=Computers,DC=lab,DC=test',
            'warning': (
                'Target OU is empty; domain default computer location is '
                'CN=Computers,DC=lab,DC=test (a container, not an OU). '
                'Windows 11 24H2/25H2 cannot join CN=Computers via -OUPath.'
            ),
        }

    monkeypatch.setattr('app.domain_credentials.host_ldap_bind', _fake_bind)
    monkeypatch.setattr('app.domain_credentials.host_ldap_check_join_ou', _fake_ou)
    resp = client.post(
        '/api/domain/test_credentials',
        json={
            'use_domain_profile_credentials': False,
            'domain_name': 'lab.test',
            'domain_username': 'svc@lab.test',
            'domain_password': 'x',
            'dns_servers': '10.0.0.1',
            'domain_ou': '',
        },
        headers={'Authorization': 'Bearer tok'},
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body['ok'] is False
    assert body['class'] == 'empty_default_container'
    assert body.get('continue_allowed') is True
    assert '24H2' in (body.get('ou_warning') or body.get('advisory') or '')
    assert body.get('default_computer_container') == 'CN=Computers,DC=lab,DC=test'


def test_api_domain_test_credentials_uses_profile_ou(client, app, monkeypatch):
    app.config['API_TOKENS'] = frozenset({'tok'})
    app.config['DOMAIN_PROFILES'] = {
        'Lab': {
            'dns_servers': '10.0.0.1',
            'domain_name': 'lab.test',
            'domain_username': 'svc@lab.test',
            'domain_password': 's3cret!',
            'domain_ou': 'OU=VDI,DC=lab,DC=test',
        }
    }

    def _fake_bind(data, timeout=5.0):
        return {
            'ok': True,
            'class': 'ok',
            'result': 'OK',
            'bind_target': '10.0.0.1:389',
            'domain': 'lab.test',
            'username': 'svc@lab.test',
        }

    seen = {}

    def _fake_ou(data, timeout=5.0):
        seen['ou'] = data.get('domain_ou')
        return {
            'ok': True,
            'class': 'ok',
            'result': 'OU OK',
            'skipped': False,
            'domain_ou': data.get('domain_ou'),
            'default_computer_container': None,
            'warning': None,
        }

    monkeypatch.setattr('app.domain_credentials.host_ldap_bind', _fake_bind)
    monkeypatch.setattr('app.domain_credentials.host_ldap_check_join_ou', _fake_ou)
    resp = client.post(
        '/api/domain/test_credentials',
        json={
            'domain_profile': 'Lab',
            'use_domain_profile_credentials': True,
            'domain_ou': '',
        },
        headers={'Authorization': 'Bearer tok'},
    )
    assert resp.status_code == 200
    assert seen.get('ou') == 'OU=VDI,DC=lab,DC=test'
    body = resp.get_json()
    assert body.get('domain_ou') == 'OU=VDI,DC=lab,DC=test'
    assert 's3cret' not in str(body)


def test_credential_dns_servers_ignores_request_when_profile_creds(app):
    from app.domain_credentials import credential_dns_servers, _ldap_server_candidates

    app.config['DOMAIN_PROFILES'] = {
        'Lab': {'dns_servers': '10.0.0.10,10.0.0.11', 'domain_name': 'lab.test'},
    }
    data = {
        'domain_profile': 'Lab',
        'use_domain_profile_credentials': True,
        'dns_servers': '203.0.113.8',
        'domain_name': 'lab.test',
    }
    with app.app_context():
        assert credential_dns_servers(data) == ['10.0.0.10', '10.0.0.11']
        assert _ldap_server_candidates(data)[0] == '10.0.0.10'
        data['use_domain_profile_credentials'] = False
        assert credential_dns_servers(data) == ['203.0.113.8']
