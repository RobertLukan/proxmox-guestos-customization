import pytest

from app.validators import (
    ValidationError,
    validate_bridge,
    validate_dns_servers,
    validate_hostname,
    validate_ipv4,
    validate_mac,
    validate_netmask,
    validate_vlan,
)


def test_validate_ipv4_ok():
    assert validate_ipv4('192.168.1.10') == '192.168.1.10'


@pytest.mark.parametrize('bad', ['', 'not-an-ip', '999.1.1.1', '1.2.3.4; rm -rf /', '10.0.0'])
def test_validate_ipv4_rejects(bad):
    with pytest.raises(ValidationError):
        validate_ipv4(bad)


def test_validate_netmask_ok():
    assert validate_netmask('24') == 24
    assert validate_netmask(0) == 0
    assert validate_netmask(32) == 32


@pytest.mark.parametrize('bad', [-1, 33, 'x', None])
def test_validate_netmask_rejects(bad):
    with pytest.raises(ValidationError):
        validate_netmask(bad)


def test_validate_vlan_ok_and_empty():
    assert validate_vlan('100') == 100
    assert validate_vlan('') is None
    assert validate_vlan(None) is None


@pytest.mark.parametrize('bad', [0, 4095, 'abc'])
def test_validate_vlan_rejects(bad):
    with pytest.raises(ValidationError):
        validate_vlan(bad)


def test_validate_hostname_takes_first_label():
    assert validate_hostname('web-01.corp.local') == 'web-01'


@pytest.mark.parametrize('bad', ['', 'a' * 16, 'bad_name', 'has space', "$(evil)", 'na;me'])
def test_validate_hostname_rejects(bad):
    with pytest.raises(ValidationError):
        validate_hostname(bad)


def test_validate_mac_ok():
    assert validate_mac('AA:BB:CC:DD:EE:FF') == 'AA:BB:CC:DD:EE:FF'


@pytest.mark.parametrize('bad', ['', 'AA:BB:CC:DD:EE', 'ZZ:BB:CC:DD:EE:FF', 'no'])
def test_validate_mac_rejects(bad):
    with pytest.raises(ValidationError):
        validate_mac(bad)


def test_validate_dns_servers_parses_list():
    assert validate_dns_servers('10.0.0.1, 10.0.0.2') == ['10.0.0.1', '10.0.0.2']
    assert validate_dns_servers('') == []


def test_validate_dns_servers_rejects_bad_entry():
    with pytest.raises(ValidationError):
        validate_dns_servers('10.0.0.1, notanip')


def test_validate_bridge_ok():
    assert validate_bridge('vmbr0') == 'vmbr0'
    assert validate_bridge('vnet-prod') == 'vnet-prod'
    assert validate_bridge('') is None


@pytest.mark.parametrize('bad', ['vmbr0,tag=10', 'bad=bridge', 'has space', 'a' * 65])
def test_validate_bridge_rejects(bad):
    with pytest.raises(ValidationError):
        validate_bridge(bad)
