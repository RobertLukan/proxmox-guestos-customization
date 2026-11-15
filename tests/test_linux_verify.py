"""Direct tests for Linux verify / data-disk finalize script generation."""
from app.linux_verify import _linux_data_disk_script, _verify_mount, verify_linux_result


def test_linux_data_disk_script_uses_exact_mountpoint_and_ext4_force():
    script = _linux_data_disk_script('guestos-data1', '/data', 'ext4')
    assert "findmnt -n --mountpoint '/data'" in script
    assert '--target' not in script
    assert "mkfs.ext4 -L 'guestos-data1' -F \"$DEV\"" in script


def test_linux_data_disk_script_xfs_uses_lowercase_force():
    script = _linux_data_disk_script('guestos-data1', '/var/lib/app', 'xfs')
    assert "findmnt -n --mountpoint '/var/lib/app'" in script
    assert "mkfs.xfs -L 'guestos-data1' -f \"$DEV\"" in script
    assert 'mkfs.xfs' in script
    assert '-F' not in script


def test_verify_mount_requires_exact_mountpoint(monkeypatch):
    from app import linux_verify as lv

    seen = {}

    def _run(_vmid, cmd, **_k):
        seen['cmd'] = cmd
        return '/data\n'

    monkeypatch.setattr(lv, 'run_command_in_guest', _run)
    assert _verify_mount(1, '/data') is True
    assert '--mountpoint' in seen['cmd']
    assert '--target' not in seen['cmd']


def test_verify_linux_result_ok_when_all_data_mounts_present(monkeypatch):
    from app import linux_verify as lv

    monkeypatch.setattr(lv, '_guest_ipv4_ipv6', lambda *_a, **_k: (['192.0.2.10'], []))
    monkeypatch.setattr(lv, '_verify_linux_hostname', lambda *_a, **_k: ('linux-test', True))
    monkeypatch.setattr(lv, 'finalize_linux_data_disks', lambda *_a, **_k: None)
    monkeypatch.setattr(lv, '_verify_mount', lambda *_a, **_k: True)
    monkeypatch.setattr(lv.time, 'sleep', lambda *_a, **_k: None)

    summary, ok = verify_linux_result(
        1,
        {
            'hostname': 'linux-test',
            'use_dhcp': True,
            'disk_guest_plan': [
                {'role': 'data', 'serial': 'd1', 'mountpoint': '/data'},
                {'role': 'data', 'serial': 'd2', 'mountpoint': '/opt/data'},
            ],
        },
        timeout=1,
    )
    assert ok is True
    assert 'mount /data' in summary
    assert 'mount /opt/data' in summary
    assert 'missing' not in summary


def test_verify_linux_result_fails_only_missing_mount(monkeypatch):
    from app import linux_verify as lv

    monkeypatch.setattr(lv, '_guest_ipv4_ipv6', lambda *_a, **_k: (['192.0.2.10'], []))
    monkeypatch.setattr(lv, '_verify_linux_hostname', lambda *_a, **_k: ('linux-test', True))
    monkeypatch.setattr(lv, 'finalize_linux_data_disks', lambda *_a, **_k: None)
    monkeypatch.setattr(
        lv,
        '_verify_mount',
        lambda _v, mp: mp == '/data',
    )
    monkeypatch.setattr(lv.time, 'sleep', lambda *_a, **_k: None)

    summary, ok = verify_linux_result(
        1,
        {
            'hostname': 'linux-test',
            'use_dhcp': True,
            'disk_guest_plan': [
                {'role': 'data', 'serial': 'd1', 'mountpoint': '/data'},
                {'role': 'data', 'serial': 'd2', 'mountpoint': '/opt/data'},
            ],
        },
        timeout=1,
    )
    assert ok is False
    assert 'mount /data' in summary
    assert 'mount /opt/data missing' in summary
