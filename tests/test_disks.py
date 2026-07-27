"""Unit tests for optional disk plan validation."""
import pytest

from app.disks import prepare_disk_plan
from app.validators import ValidationError


def test_manage_disks_off_clears_plan():
    data = {'manage_disks': False, 'disks': [{'role': 'data', 'size_gb': 10}]}
    prepare_disk_plan(data)
    assert data['manage_disks'] is False
    assert data['disks'] == []
    assert data['disk_guest_plan'] == []


def test_manage_disks_default_plan():
    data = {'manage_disks': True}
    prepare_disk_plan(data)
    roles = [d['role'] for d in data['disks']]
    assert roles == ['os', 'pagefile', 'data']
    assert data['disks'][1]['serial'] == 'guestos-pagefile'
    assert data['disks'][2]['drive_letter'] == 'D'


def test_manage_disks_rejects_bad_role():
    data = {'manage_disks': True, 'disks': [{'role': 'tape', 'size_gb': 1}]}
    with pytest.raises(ValidationError):
        prepare_disk_plan(data)


def test_manage_disks_requires_one_os():
    data = {
        'manage_disks': True,
        'disks': [
            {'role': 'data', 'size_gb': 10, 'drive_letter': 'D'},
        ],
    }
    with pytest.raises(ValidationError, match='exactly one'):
        prepare_disk_plan(data)


def test_parse_boot_disk_options_helpers():
    from app.proxmox import _parse_disk_value, _format_disk_value, _parse_size_to_gb

    assert _parse_size_to_gb('32G') == 32
    assert _parse_size_to_gb('2048M') == 2
    parsed = _parse_disk_value('local-lvm:vm-1-disk-0,discard=on,aio=io_uring,size=40G,ssd=1')
    assert parsed['storage'] == 'local-lvm'
    assert parsed['size_gb'] == 40
    assert parsed['opts']['discard'] == 'on'
    assert parsed['opts']['aio'] == 'io_uring'
    formatted = _format_disk_value('local-lvm', 16, {'discard': 'on', 'aio': 'io_uring'}, serial='guestos-pagefile')
    assert formatted.startswith('local-lvm:16,')
    assert 'serial=guestos-pagefile' in formatted
    assert 'discard=on' in formatted
    assert 'aio=io_uring' in formatted


def test_cdrom_iso_detection_keeps_media_in_opts():
    from app.proxmox import _parse_disk_value, _is_cdrom_or_iso

    iso = _parse_disk_value('local:iso/virtio-win.iso,media=cdrom')
    assert iso['opts'].get('media') == 'cdrom'
    assert _is_cdrom_or_iso(iso, 'local:iso/virtio-win.iso,media=cdrom')
    # ISO path without media= still detected (PVE rejects rewriting these).
    bare = _parse_disk_value('local:iso/virtio-win.iso')
    assert _is_cdrom_or_iso(bare, 'local:iso/virtio-win.iso')
    disk = _parse_disk_value('local-lvm:vm-1-disk-0,size=40G,serial=boot')
    assert disk['opts'].get('serial') == 'boot'
    assert not _is_cdrom_or_iso(disk, 'local-lvm:vm-1-disk-0,size=40G,serial=boot')


def test_matched_disk_inherits_boot_copy_opts():
    """Template secondary disks (e.g. scsi1 with only iothread) get boot opts."""
    from app.proxmox import _build_disk_config_value

    raw = 'data15_nvme:vm-1-disk-1,iothread=1,size=4G'
    copy_opts = {'backup': '0', 'discard': 'on', 'iothread': '1', 'ssd': '1'}
    out = _build_disk_config_value(raw, serial='guestos-pagefile', extra_opts=copy_opts)
    assert out.startswith('data15_nvme:vm-1-disk-1,')
    assert 'size=4G' in out
    assert 'serial=guestos-pagefile' in out
    assert 'discard=on' in out
    assert 'ssd=1' in out
    assert 'backup=0' in out
    assert 'iothread=1' in out
