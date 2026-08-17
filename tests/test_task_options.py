"""Tests for sanitized task options snapshots."""
from app.task_options import build_task_options, options_summary_chips, options_to_json


def test_build_task_options_strips_secrets():
    data = {
        'network_mode': 'dhcp',
        'join_domain': True,
        'domain_name': 'lab.test',
        'domain_username': 'svc@lab.test',
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
    assert opts['domain_username'] == 'svc@lab.test'
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
    assert 'svc@lab.test' in raw


def test_build_task_options_records_effective_ou():
    """Job history must show the OU actually used (profile backfill included)."""
    from app.task_options import join_summary_lines

    opts = build_task_options({
        'join_domain': True,
        'domain_name': 'test.test.org',
        'domain_profile': 'prod',
        'domain_ou': 'CN=Computers,DC=test,DC=test,DC=org',
        'domain_join_method': 'add-computer',
    })
    assert opts['domain_ou'] == 'CN=Computers,DC=test,DC=test,DC=org'
    lines = join_summary_lines(opts)
    assert 'Target OU CN=Computers,DC=test,DC=test,DC=org' in lines
    assert any('default Computers container' in ln for ln in lines)

    blank = build_task_options({
        'join_domain': True,
        'domain_name': 'lab.test',
        'domain_join_method': 'add-computer',
    })
    assert 'domain_ou' not in blank
    assert 'Target OU (none) — default computer container' in join_summary_lines(blank)


def test_build_task_options_join_path_chips():
    opts = build_task_options({
        'network_mode': 'dhcp',
        'join_domain': True,
        'domain_join_method': 'odj',
        'host_dc_reachable': True,
        'host_dc_target': '192.168.123.191:389',
    })
    assert opts['domain_join_method'] == 'odj'
    assert opts['host_dc_reachable'] is True
    chips = options_summary_chips(opts)
    assert 'ODJ' in chips
    assert 'host-DC-down' not in chips

    late = build_task_options({
        'join_domain': True,
        'domain_join_method': 'add-computer',
        'host_dc_reachable': False,
    })
    chips = options_summary_chips(late)
    assert 'late-AD' in chips
    assert 'host-DC-down' in chips


def test_join_summary_lines_odj_and_late():
    from app.task_options import join_summary_lines

    odj = join_summary_lines({
        'join_domain': True,
        'domain_name': 'lab.test',
        'domain_join_method': 'odj',
        'host_dc_reachable': True,
        'host_dc_target': '192.168.123.191:389',
    })
    assert odj[0] == 'Domain lab.test'
    assert 'Offline Domain Join (ODJ) at specialize' in odj
    assert 'GuestOS host DC reachable (192.168.123.191:389)' in odj

    late = join_summary_lines({
        'join_domain': True,
        'domain_join_method': 'add-computer',
        'host_dc_reachable': False,
    })
    assert 'late Add-Computer after OOBE' in late
    assert any('unreachable' in line for line in late)
    assert join_summary_lines({'join_domain': False}) == []
