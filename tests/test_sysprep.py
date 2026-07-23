import pytest

from app.celery_app import _validate_sysprep_network
from app.validators import ValidationError


def test_validate_sysprep_network_normalizes():
    data = {
        'ip_address': '192.168.1.5',
        'netmask_cidr': '24',
        'gateway': '192.168.1.1',
        'dns_servers': '10.0.0.1,10.0.0.2',
        'hostname': 'web-01.corp.local',
    }
    _validate_sysprep_network(data)
    assert data['netmask_cidr'] == 24          # normalized to int
    assert data['hostname'] == 'web-01'         # first label only


@pytest.mark.parametrize('field,value', [
    ('ip_address', "1.2.3.4'; Remove-Item C:\\ #"),
    ('gateway', 'not-an-ip'),
    ('netmask_cidr', '99'),
    ('dns_servers', '10.0.0.1, evil'),
])
def test_validate_sysprep_network_rejects_bad_input(field, value):
    data = {
        'ip_address': '192.168.1.5',
        'netmask_cidr': '24',
        'gateway': '192.168.1.1',
        'dns_servers': '10.0.0.1',
    }
    data[field] = value
    with pytest.raises(ValidationError):
        _validate_sysprep_network(data)
