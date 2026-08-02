"""Tests for sanitized task options snapshots."""
from app.task_options import build_task_options, options_summary_chips, options_to_json


def test_build_task_options_strips_secrets():
    data = {
        'network_mode': 'dhcp',
        'join_domain': True,
        'domain_name': 'lab.test',
        'domain_profile': 'lab',
        'administrator_password': 'nope',
        'domain_password': 'nope',
        'domain_join_b64': 'abc',
        'manage_disks': True,
        'disks': [{'role': 'pagefile', 'size_gb': 8}],
        'bridge': 'vmbr0',
        'cores': 2,
        'ram': 4096,
    }
    opts = build_task_options(data)
    assert opts['network_mode'] == 'dhcp'
    assert opts['join_domain'] is True
    assert opts['domain_name'] == 'lab.test'
    assert 'administrator_password' not in opts
    assert 'domain_password' not in opts
    assert 'domain_join_b64' not in opts
    assert opts['disks'][0]['role'] == 'pagefile'
    chips = options_summary_chips(opts)
    assert 'DHCP' in chips
    assert 'AD' in chips
    assert 'disks' in chips
    raw = options_to_json(data)
    assert 'nope' not in raw
