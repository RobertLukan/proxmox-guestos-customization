"""Unit tests for Linux cloud-init payload and vendor-data helpers."""
from __future__ import annotations

import pytest

from app.linux_cloudinit import prepare_linux_payload, render_linux_vendor_data
from app.validators import ValidationError, validate_linux_hostname


def test_validate_linux_hostname_ok():
    assert validate_linux_hostname('linux-app-01') == 'linux-app-01'
    assert validate_linux_hostname('Ubuntu.example.com') == 'ubuntu'


@pytest.mark.parametrize('bad', ['', '-bad', 'bad-', 'a' * 64, 'has space'])
def test_validate_linux_hostname_rejects(bad):
    with pytest.raises(ValidationError):
        validate_linux_hostname(bad)


def test_prepare_linux_payload_static():
    data = {
        'hostname': 'web-01',
        'cores': 2,
        'ram': 4096,
        'bridge': 'vmbr0',
        'network_mode': 'static',
        'ip_address': '192.168.123.50',
        'netmask': 24,
        'gateway': '192.168.123.1',
        'dns_servers': '192.168.123.191',
        'ciuser': 'ubuntu',
    }
    prepare_linux_payload(data)
    assert data['hostname'] == 'web-01'
    assert data['use_dhcp'] is False
    assert data['nics'][0]['ip_address'] == '192.168.123.50'
    assert data['dns_list'] == ['192.168.123.191']
    assert data['manage_disks'] is False


def test_prepare_linux_payload_dhcp():
    data = {
        'hostname': 'dhcp-01',
        'cores': 2,
        'ram': 2048,
        'network_mode': 'dhcp',
    }
    prepare_linux_payload(data)
    assert data['use_dhcp'] is True
    assert data['nics'][0]['network_mode'] == 'dhcp'


def test_prepare_linux_manage_disks_requires_plan():
    data = {
        'hostname': 'disk-01',
        'cores': 2,
        'ram': 2048,
        'network_mode': 'dhcp',
        'manage_disks': True,
    }
    with pytest.raises(ValidationError):
        prepare_linux_payload(data)


def test_prepare_linux_os_disk_gb():
    data = {
        'hostname': 'grow-01',
        'cores': 2,
        'ram': 2048,
        'network_mode': 'dhcp',
        'os_disk_gb': 40,
    }
    prepare_linux_payload(data)
    assert data['manage_disks'] is True
    assert data['os_disk_gb'] == 40
    assert data['disks'][0]['role'] == 'os'
    assert data['disks'][0]['grow_to_gb'] == 40


def test_prepare_linux_detach_flag():
    data = {
        'hostname': 'freeze-01',
        'cores': 2,
        'ram': 2048,
        'network_mode': 'dhcp',
        'detach_cloudinit_after_ready': True,
    }
    prepare_linux_payload(data)
    assert data['detach_cloudinit_after_ready'] is True


def test_prepare_linux_ipv6_only_nic():
    data = {
        'hostname': 'v6-only',
        'cores': 2,
        'ram': 2048,
        'network_mode': 'dhcp',
        'nics': [{
            'bridge': 'vmbr0',
            'network_mode': 'dhcp',
            'enable_ipv6': True,
            'ipv6_mode': 'static',
            'ipv6_address': 'fd00::10',
            'ipv6_prefix': 64,
            'ipv6_gateway': 'fd00::1',
        }],
    }
    prepare_linux_payload(data)
    nic = data['nics'][0]
    assert nic['enable_ipv6'] is True
    assert nic['ipv6_address'] == 'fd00::10'
    assert nic['ipv6_prefix'] == 64
    assert nic['ipv6_gateway'] == 'fd00::1'
    assert nic['network_mode'] == 'dhcp'


def test_prepare_linux_dual_stack_nic():
    data = {
        'hostname': 'dual-stack',
        'cores': 2,
        'ram': 2048,
        'network_mode': 'static',
        'nics': [{
            'bridge': 'vmbr0',
            'network_mode': 'static',
            'ip_address': '10.0.0.5',
            'netmask': 24,
            'gateway': '10.0.0.1',
            'enable_ipv6': True,
            'ipv6_mode': 'static',
            'ipv6_address': '2001:db8::5',
            'ipv6_prefix': 48,
            'ipv6_gateway': '2001:db8::1',
        }],
    }
    prepare_linux_payload(data)
    nic = data['nics'][0]
    assert nic['ip_address'] == '10.0.0.5'
    assert nic['ipv6_address'] == '2001:db8::5'
    assert nic['ipv6_prefix'] == 48
    assert data['enable_ipv6'] is True


def test_prepare_linux_multi_nic_2():
    data = {
        'hostname': 'multi-nic',
        'cores': 2,
        'ram': 2048,
        'network_mode': 'static',
        'nics': [
            {
                'bridge': 'vmbr0',
                'network_mode': 'static',
                'ip_address': '192.168.1.10',
                'netmask': 24,
                'gateway': '192.168.1.1',
            },
            {
                'bridge': 'vmbr1',
                'network_mode': 'dhcp',
            },
        ],
    }
    prepare_linux_payload(data)
    assert len(data['nics']) == 2
    assert data['nics'][0]['ip_address'] == '192.168.1.10'
    assert data['nics'][1]['network_mode'] == 'dhcp'
    assert data['ip_address'] == '192.168.1.10'


def test_prepare_linux_multi_nic_4():
    nics = [
        {'bridge': f'vmbr{i}', 'network_mode': 'dhcp'}
        for i in range(4)
    ]
    data = {
        'hostname': 'four-nic',
        'cores': 2,
        'ram': 2048,
        'network_mode': 'dhcp',
        'nics': nics,
    }
    prepare_linux_payload(data)
    assert len(data['nics']) == 4
    for i, nic in enumerate(data['nics']):
        assert nic['bridge'] == f'vmbr{i}'


def test_prepare_linux_accepts_netmask_cidr_alias():
    data = {
        'hostname': 'cidr-alias',
        'cores': 2,
        'ram': 2048,
        'network_mode': 'static',
        'nics': [{
            'bridge': 'vmbr0',
            'network_mode': 'static',
            'ip_address': '10.0.0.8',
            'netmask_cidr': 24,
            'gateway': '10.0.0.1',
        }],
    }
    prepare_linux_payload(data)
    assert data['nics'][0]['netmask'] == 24


def test_prepare_linux_ipv6_dhcp_mode():
    data = {
        'hostname': 'v6dhcp',
        'cores': 2,
        'ram': 2048,
        'network_mode': 'dhcp',
        'nics': [{
            'bridge': 'vmbr0',
            'network_mode': 'dhcp',
            'enable_ipv6': True,
            'ipv6_mode': 'dhcp',
        }],
    }
    prepare_linux_payload(data)
    nic = data['nics'][0]
    assert nic['ipv6_mode'] == 'dhcp'
    assert nic['ipv6_address'] == ''


def test_render_vendor_data_with_data_disk():
    plan = [{
        'role': 'data',
        'serial': 'guestos-data1',
        'mountpoint': '/data',
        'fstype': 'ext4',
    }]
    text = render_linux_vendor_data({}, disk_guest_plan=plan)
    assert 'disk_setup:' in text
    assert 'guestos-data1' in text
    assert '/data' in text
