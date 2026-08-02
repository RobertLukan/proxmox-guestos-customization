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
    assert data['disks'][1]['reformat'] is False
    assert data['disks'][2]['drive_letter'] == 'D'
    assert data['disks'][2]['reformat'] is False


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


def test_next_bus_index_skips_occupied():
    from app.proxmox import _next_bus_index

    disks = [
        {'bus': 'scsi', 'index': 0},
        {'bus': 'scsi', 'index': 1},
    ]
    assert _next_bus_index(disks, 'scsi') == 2


def test_reconcile_allocates_distinct_bus_keys_without_relist(monkeypatch):
    """Two new disks must not both land on scsi1 (which orphans the first to unused0)."""
    from app import proxmox as px

    posts = []

    class _Cfg:
        def post(self, **kwargs):
            posts.append(kwargs)

    class _Qemu:
        def __init__(self):
            self.config = _Cfg()

    class _Nodes:
        def qemu(self, vmid):
            return _Qemu()

    class _Proxmox:
        def nodes(self, node):
            return _Nodes()

    boot_disks = [{
        'key': 'scsi0',
        'bus': 'scsi',
        'index': 0,
        'storage': 'data15_nvme',
        'opts': {'discard': 'on', 'iothread': '1', 'ssd': '1'},
        'size_gb': 64,
        'raw': 'data15_nvme:vm-1-disk-1,discard=on,iothread=1,size=64G,ssd=1',
        'serial': None,
        'head': 'data15_nvme:vm-1-disk-1',
        'volume': 'vm-1-disk-1',
    }]

    def _boot(vmid):
        return {
            'key': 'scsi0',
            'bus': 'scsi',
            'index': 0,
            'storage': 'data15_nvme',
            'opts': {'discard': 'on', 'iothread': '1', 'ssd': '1'},
            'size_gb': 64,
            'raw': boot_disks[0]['raw'],
            'disks': list(boot_disks),
            'node': 'pve',
            'proxmox': _Proxmox(),
            'cfg': {'efidisk0': 'data15_nvme:vm-1-disk-0,efitype=4m,size=4M'},
        }

    monkeypatch.setattr(px, 'get_boot_disk_spec', _boot)
    # If code still re-lists and gets a stale empty secondary list, in-memory
    # tracking must still allocate scsi1 then scsi2.
    monkeypatch.setattr(
        px,
        'list_vm_disks',
        lambda vmid: (list(boot_disks), {}, 'pve', _Proxmox()),
    )
    monkeypatch.setattr(px, '_set_disk_serial', lambda *a, **k: None)

    plan = [
        {'role': 'os', 'serial': 'guestos-os', 'drive_letter': 'C', 'min_size_gb': 64},
        {
            'role': 'pagefile',
            'serial': 'guestos-pagefile',
            'drive_letter': 'P',
            'size_gb': 16,
            'ensure_pagefile': True,
            'reformat': True,
        },
        {
            'role': 'data',
            'serial': 'guestos-data-0',
            'drive_letter': 'D',
            'size_gb': 50,
            'ensure_pagefile': False,
            'reformat': False,
            'label': 'Data',
        },
    ]
    guest = px.reconcile_vm_disks(99, plan)
    keys = [p['pve_key'] for p in guest]
    assert keys == ['scsi0', 'scsi1', 'scsi2']
    assert len(posts) == 2
    assert 'scsi1' in posts[0]
    assert 'scsi2' in posts[1]
    assert 'serial=guestos-pagefile' in posts[0]['scsi1']
    assert 'serial=guestos-data-0' in posts[1]['scsi2']
    assert 'efidisk' not in ''.join(keys)
