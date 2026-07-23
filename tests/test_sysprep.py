"""Tests for the sysprep template rendering and network validation.

These lock in the behaviour that the sysprep workflow actually applies the
hostname (unattend ComputerName) and static network config (setup.ps1), and
that untrusted values are validated before being rendered into the non-escaped
PowerShell script.
"""
import pytest

from app import app as flask_app
from app.celery_app import _validate_sysprep_network, _render_sysprep_files
from app.validators import ValidationError


def _base_data():
    return {
        'hostname': 'WINSRV19-01',
        'timezone': 'Central European Standard Time',
        'administrator_password': 'Sup3r$ecret',
        'ip_address': '10.0.5.20',
        'netmask_cidr': '24',
        'gateway': '10.0.5.1',
        'dns_servers': '10.0.5.1, 8.8.8.8',
        'primary_mac_address': 'BC:24:11:AA:BB:CC',
    }


def test_validate_builds_dns_list_and_normalizes():
    data = _base_data()
    data['hostname'] = 'web-01.corp.local'
    _validate_sysprep_network(data)
    assert data['dns_list'] == ['10.0.5.1', '8.8.8.8']
    assert data['netmask_cidr'] == 24  # coerced to int
    assert data['hostname'] == 'web-01'  # first DNS label only


@pytest.mark.parametrize('field,value', [
    ('ip_address', '10.0.5.999'),
    ('ip_address', "1.2.3.4'; Remove-Item C:\\ #"),  # injection attempt
    ('gateway', 'not-an-ip'),
    ('netmask_cidr', '40'),
    ('dns_servers', '10.0.5.1, bogus'),
    ('primary_mac_address', 'ZZ:ZZ'),
])
def test_validate_rejects_bad_input(field, value):
    data = _base_data()
    data[field] = value
    with pytest.raises(ValidationError):
        _validate_sysprep_network(data)


def test_unattend_sets_hostname_and_timezone():
    data = _base_data()
    with flask_app.app_context():
        _validate_sysprep_network(data)
        xml, _ps1, _cmd = _render_sysprep_files(data)
    xml = xml.decode()
    assert '<ComputerName>WINSRV19-01</ComputerName>' in xml
    assert '<TimeZone>Central European Standard Time</TimeZone>' in xml


def test_setup_ps1_contains_network_config():
    data = _base_data()
    with flask_app.app_context():
        _validate_sysprep_network(data)
        _xml, ps1, _cmd = _render_sysprep_files(data)
    ps1 = ps1.decode()
    assert "$ip      = '10.0.5.20'" in ps1
    assert "$prefix  = 24" in ps1
    assert "$gateway = '10.0.5.1'" in ps1
    assert "@('10.0.5.1', '8.8.8.8')" in ps1
    # MAC is normalized to dash-separated uppercase inside PowerShell.
    assert "'BC:24:11:AA:BB:CC'.Replace(':', '-').ToUpper()" in ps1


def test_setup_complete_invokes_setup_ps1():
    data = _base_data()
    with flask_app.app_context():
        _validate_sysprep_network(data)
        _xml, _ps1, cmd = _render_sysprep_files(data)
    cmd = cmd.decode()
    assert 'setup.ps1' in cmd
    assert 'powershell.exe' in cmd
