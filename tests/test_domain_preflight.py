"""Unit tests for AD domain-join operational preflight."""

import pytest

from app.domain_preflight import check_domain_join_preflight
from app.validators import ValidationError


def test_preflight_skips_when_join_not_requested():
    assert check_domain_join_preflight({'join_domain': False}) is None
    assert check_domain_join_preflight({}) is None


def test_preflight_requires_targets_when_joining():
    with pytest.raises(ValidationError, match='no dns_servers'):
        check_domain_join_preflight({
            'join_domain': True,
            'domain_name': '',
            'dns_servers': '',
        })


def test_preflight_passes_when_any_port_open(monkeypatch):
    calls = []

    def _reachable(host, port, timeout):
        calls.append((host, port))
        return host == '10.0.0.2' and port == 389

    monkeypatch.setattr('app.domain_preflight._tcp_reachable', _reachable)
    assert check_domain_join_preflight({
        'join_domain': True,
        'dns_servers': '10.0.0.2',
        'domain_name': 'lab.test',
    }) is None
    assert ('10.0.0.2', 389) in calls


def test_preflight_fails_when_all_unreachable(monkeypatch):
    monkeypatch.setattr(
        'app.domain_preflight._tcp_reachable',
        lambda *_a, **_k: False,
    )
    with pytest.raises(ValidationError, match='no domain controller'):
        check_domain_join_preflight({
            'join_domain': True,
            'dns_servers': '10.0.0.99',
            'domain_name': 'lab.test',
        })
