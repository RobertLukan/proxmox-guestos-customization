"""Tests for domain-profile sanitization and server-side credential resolution."""
from app import app as flask_app
from app.routes import sanitized_domain_profiles, resolve_domain_join_from_request


PROFILES = {
    'Lab': {
        'dns_servers': '10.0.0.10,10.0.0.11',
        'domain_name': 'lab.example.com',
        'domain_username': 'svc-join@lab.example.com',
        'domain_password': 's3cret!',
        'domain_ou': 'OU=Servers,DC=lab,DC=example,DC=com',
        'vlan': 100,
    }
}


def test_sanitized_domain_profiles_strip_credentials(app):
    flask_app.config['DOMAIN_PROFILES'] = PROFILES
    sanitized = sanitized_domain_profiles()
    assert 'Lab' in sanitized
    assert sanitized['Lab']['domain_name'] == 'lab.example.com'
    assert sanitized['Lab']['dns_servers'] == '10.0.0.10,10.0.0.11'
    assert 'domain_password' not in sanitized['Lab']
    assert 'domain_username' not in sanitized['Lab']


def test_profile_fills_dns_and_vlan_without_join(app):
    """Domain profile is a network shortcut — works without Join Domain."""
    flask_app.config['DOMAIN_PROFILES'] = PROFILES
    data = {
        'join_domain': False,
        'domain_profile': 'Lab',
        'dns_servers': '',
        'vlan': '',
    }
    ok, err = resolve_domain_join_from_request(data)
    assert ok and err is None
    assert data['dns_servers'] == '10.0.0.10,10.0.0.11'
    assert data['vlan'] == 100
    assert data['join_domain'] is False
    assert 'domain_password' not in data


def test_profile_does_not_overwrite_existing_dns_vlan(app):
    """Non-blank DNS/VLAN from the request win over profile defaults."""
    flask_app.config['DOMAIN_PROFILES'] = PROFILES
    data = {
        'join_domain': False,
        'domain_profile': 'Lab',
        'dns_servers': '8.8.8.8',
        'vlan': 42,
    }
    ok, err = resolve_domain_join_from_request(data)
    assert ok and err is None
    assert data['dns_servers'] == '8.8.8.8'
    assert data['vlan'] == 42
    assert data['join_domain'] is False


def test_resolve_domain_join_from_profile(app):
    flask_app.config['DOMAIN_PROFILES'] = PROFILES
    data = {
        'join_domain': True,
        'use_domain_profile_credentials': True,
        'domain_profile': 'Lab',
        # browser must not be trusted for secrets when using a profile
        'domain_password': 'attacker-supplied',
        'domain_username': 'attacker',
        'domain_name': 'evil.local',
        'dns_servers': '',
        'vlan': '',
    }
    ok, err = resolve_domain_join_from_request(data)
    assert ok and err is None
    assert data['domain_name'] == 'lab.example.com'
    assert data['domain_username'] == 'svc-join@lab.example.com'
    assert data['domain_password'] == 's3cret!'
    assert data['dns_servers'] == '10.0.0.10,10.0.0.11'
    assert data['vlan'] == 100
    assert data['domain_ou'] == 'OU=Servers,DC=lab,DC=example,DC=com'


def test_resolve_domain_join_unknown_profile(app):
    flask_app.config['DOMAIN_PROFILES'] = PROFILES
    data = {
        'join_domain': True,
        'use_domain_profile_credentials': True,
        'domain_profile': 'Missing',
    }
    with flask_app.app_context():
        ok, err = resolve_domain_join_from_request(data)
    assert not ok
    response, status = err
    assert status == 400
    body = response.get_json()
    assert 'error' in body
    assert 'errors' in body
    assert 'domain_profile' in body['errors']


def test_resolve_domain_join_requires_profile_when_using_profile_creds(app):
    flask_app.config['DOMAIN_PROFILES'] = PROFILES
    data = {
        'join_domain': True,
        'use_domain_profile_credentials': True,
        'domain_profile': '',
    }
    with flask_app.app_context():
        ok, err = resolve_domain_join_from_request(data)
    assert not ok
    response, status = err
    assert status == 400
    body = response.get_json()
    assert 'domain_profile' in body.get('errors', {})
    assert 'domain profile' in body['errors']['domain_profile'].lower()


def test_resolve_bulk_skips_profile_dns_vlan(app):
    """Bulk: profile credentials without applying profile DNS/VLAN (DHCP/CSV own network)."""
    flask_app.config['DOMAIN_PROFILES'] = PROFILES
    data = {
        'join_domain': True,
        'use_domain_profile_credentials': True,
        'domain_profile': 'Lab',
        'dns_servers': '',
        'vlan': '',
        'domain_ou': '',
    }
    ok, err = resolve_domain_join_from_request(data, apply_network=False)
    assert ok and err is None
    assert data['domain_name'] == 'lab.example.com'
    assert data['domain_username'] == 'svc-join@lab.example.com'
    assert data['domain_password'] == 's3cret!'
    assert data['dns_servers'] == ''
    assert data['vlan'] == ''
    assert data['domain_ou'] == 'OU=Servers,DC=lab,DC=example,DC=com'


def test_resolve_bulk_keeps_explicit_dns(app):
    flask_app.config['DOMAIN_PROFILES'] = PROFILES
    data = {
        'join_domain': True,
        'use_domain_profile_credentials': True,
        'domain_profile': 'Lab',
        'dns_servers': '192.168.123.191',
        'vlan': '',
    }
    ok, err = resolve_domain_join_from_request(data, apply_network=False)
    assert ok and err is None
    assert data['dns_servers'] == '192.168.123.191'
    assert data['vlan'] == ''
    assert data['domain_password'] == 's3cret!'


def test_resolve_domain_join_manual_credentials(app):
    flask_app.config['DOMAIN_PROFILES'] = PROFILES
    data = {
        'join_domain': True,
        'use_domain_profile_credentials': False,
        'domain_name': 'manual.local',
        'domain_username': 'manual-user',
        'domain_password': 'manual-pass',
        'domain_ou': 'OU=Custom,DC=manual,DC=local',
    }
    ok, err = resolve_domain_join_from_request(data)
    assert ok and err is None
    assert data['domain_name'] == 'manual.local'
    assert data['domain_username'] == 'manual-user'
    assert data['domain_password'] == 'manual-pass'


def test_resolve_no_join_clears_password(app):
    data = {'join_domain': False, 'domain_password': 'leftover'}
    ok, err = resolve_domain_join_from_request(data)
    assert ok and err is None
    assert data['join_domain'] is False
    assert 'domain_password' not in data
