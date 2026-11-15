"""Appliance payload checks. These tests do not clone a VM."""
from app.appliance import opnsense_setup_script, prepare_appliance_payload
from app.validators import ValidationError
import pytest


def _payload(**overrides):
    data = {
        'hostname': 'fw-01',
        'template_vmid': 200,
        'cores': 2,
        'ram': 2048,
        'nics': [
            {
                'role': 'wan',
                'bridge': 'vmbr0',
                'network_mode': 'static',
                'ip_address': '192.168.123.40',
                'netmask': 24,
                'gateway': '192.168.123.1',
            },
            {
                'role': 'lan',
                'bridge': 's0000001',
                'network_mode': 'static',
                'ip_address': '10.20.0.1',
                'netmask': 24,
            },
        ],
    }
    data.update(overrides)
    return data


def test_prepare_appliance_keeps_two_untagged_nics():
    data = prepare_appliance_payload(_payload())
    assert data['nat'] is True
    assert [nic['role'] for nic in data['nics']] == ['wan', 'lan']
    assert all(nic['vlan'] is None for nic in data['nics'])
    script = opnsense_setup_script(data['nics'])
    assert "config['nat']['outbound']['mode'] = 'automatic'" in script
    assert '192.168.123.40' in script
    assert '10.20.0.1' in script
    assert 'config.inc is not on this template' in script


def test_prepare_appliance_normalizes_lan_first_to_wan_then_lan():
    """Request order must not flip OPNsense vtnet0/vtnet1 mapping."""
    from app.linux_cloudinit import clone_nic_list

    data = _payload()
    data['nics'] = [data['nics'][1], data['nics'][0]]  # LAN, WAN
    prepared = prepare_appliance_payload(data)
    assert [nic['role'] for nic in prepared['nics']] == ['wan', 'lan']
    assert prepared['bridge'] == 'vmbr0'
    assert prepared['nics'][0]['ip_address'] == '192.168.123.40'
    assert prepared['nics'][1]['ip_address'] == '10.20.0.1'
    clone_nics = clone_nic_list(prepared)
    assert clone_nics[0]['bridge'] == 'vmbr0'
    assert clone_nics[1]['bridge'] == 's0000001'
    script = opnsense_setup_script(prepared['nics'])
    assert "$config['interfaces']['wan']['if'] = 'vtnet0';" in script
    assert "$config['interfaces']['lan']['if'] = 'vtnet1';" in script
    assert '192.168.123.40' in script
    assert '10.20.0.1' in script


def test_prepare_appliance_rejects_vlan():
    data = _payload()
    data['nics'][1]['vlan'] = 10
    with pytest.raises(ValidationError, match='VLAN'):
        prepare_appliance_payload(data)


def test_prepare_appliance_rejects_one_nic():
    data = _payload()
    data['nics'] = data['nics'][:1]
    with pytest.raises(ValidationError, match='WAN NIC and a LAN NIC'):
        prepare_appliance_payload(data)
