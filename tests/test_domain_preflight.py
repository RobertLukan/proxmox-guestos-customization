"""Unit tests for AD domain-join operational preflight."""

from app.domain_preflight import check_domain_join_preflight


def test_preflight_skips_when_join_not_requested():
    assert check_domain_join_preflight({'join_domain': False}) is None
    assert check_domain_join_preflight({}) is None


def test_preflight_warns_when_no_targets():
    warn = check_domain_join_preflight({
        'join_domain': True,
        'domain_name': '',
        'dns_servers': '',
    })
    assert warn is not None
    assert 'in-clone' in warn.lower() or 'continuing' in warn.lower()


def test_preflight_passes_when_any_port_open(monkeypatch):
    calls = []

    def _reachable(host, port, timeout):
        calls.append((host, port))
        return host == '10.0.0.2' and port == 389

    monkeypatch.setattr('app.domain_preflight._tcp_reachable', _reachable)
    data = {
        'join_domain': True,
        'dns_servers': '10.0.0.2',
        'domain_name': 'lab.test',
    }
    assert check_domain_join_preflight(data) is None
    assert ('10.0.0.2', 389) in calls
    assert data['host_dc_reachable'] is True
    assert data['host_dc_target'] == '10.0.0.2:389'


def test_preflight_warns_when_all_unreachable(monkeypatch):
    monkeypatch.setattr(
        'app.domain_preflight._tcp_reachable',
        lambda *_a, **_k: False,
    )
    data = {
        'join_domain': True,
        'dns_servers': '10.0.0.99',
        'domain_name': 'lab.test',
    }
    warn = check_domain_join_preflight(data)
    assert warn is not None
    assert 'not reachable' in warn.lower() or 'continuing' in warn.lower()
    assert 'in-clone' in warn.lower()
    assert data['host_dc_reachable'] is False


def test_preflight_profile_creds_ignore_request_dns(app, monkeypatch):
    seen = []

    def _reachable(host, port, timeout):
        seen.append(host)
        return False

    monkeypatch.setattr('app.domain_preflight._tcp_reachable', _reachable)
    app.config['DOMAIN_PROFILES'] = {
        'Lab': {'dns_servers': '10.0.0.10', 'domain_name': 'lab.test'},
    }
    data = {
        'join_domain': True,
        'domain_profile': 'Lab',
        'use_domain_profile_credentials': True,
        'dns_servers': '203.0.113.8',
        'domain_name': 'lab.test',
    }
    with app.app_context():
        check_domain_join_preflight(data)
    assert '203.0.113.8' not in seen
    assert '10.0.0.10' in seen
