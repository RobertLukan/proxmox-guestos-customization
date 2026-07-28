"""Unit tests for provisioning quotas and resource caps."""
from __future__ import annotations

import pytest

from app import db
from app.models import Task
from app.provision_limits import (
    validate_resource_caps,
    check_daily_quota,
    evaluate_storage_usage,
    check_storage_for_template,
    requested_disk_gb,
    provision_limits_snapshot,
)
from app.validators import ValidationError
import app.proxmox as pm

pytestmark = pytest.mark.api


def test_requested_disk_gb_sums_manage_disks_only():
    assert requested_disk_gb({'manage_disks': False, 'disks': [{'size_gb': 999}]}) == 0
    assert requested_disk_gb({
        'manage_disks': True,
        'disks': [
            {'role': 'os', 'grow_to_gb': 100},
            {'role': 'data', 'size_gb': 200},
            {'role': 'pagefile', 'size_gb': 16},
        ],
    }) == 316


def test_validate_resource_caps_win11(app):
    with app.app_context():
        app.config['WIN11_MAX_CORES'] = 8
        app.config['WIN11_MAX_RAM_MB'] = 65536
        app.config['WIN11_MAX_DISK_GB'] = 600
        validate_resource_caps({'cores': 8, 'ram': 65536, 'manage_disks': False}, 'win11')
        with pytest.raises(ValidationError, match='core limit'):
            validate_resource_caps({'cores': 9, 'ram': 4096, 'manage_disks': False}, 'win11')
        with pytest.raises(ValidationError, match='RAM limit'):
            validate_resource_caps({'cores': 2, 'ram': 65537, 'manage_disks': False}, 'win11')
        with pytest.raises(ValidationError, match='disk limit'):
            validate_resource_caps({
                'cores': 2,
                'ram': 4096,
                'manage_disks': True,
                'disks': [{'size_gb': 601}],
            }, 'win11')


def test_validate_resource_caps_server(app):
    with app.app_context():
        app.config['SERVER_MAX_CORES'] = 16
        app.config['SERVER_MAX_DISK_GB'] = 2048
        validate_resource_caps({'cores': 16, 'ram': 65536, 'manage_disks': False}, 'server')
        with pytest.raises(ValidationError, match='core limit'):
            validate_resource_caps({'cores': 17, 'ram': 4096, 'manage_disks': False}, 'server')
        with pytest.raises(ValidationError, match='disk limit'):
            validate_resource_caps({
                'cores': 4,
                'ram': 8192,
                'manage_disks': True,
                'disks': [{'grow_to_gb': 1024}, {'size_gb': 1025}],
            }, 'server')


def test_daily_quota(app):
    with app.app_context():
        app.config['PROVISION_MAX_PER_DAY'] = 2
        for i in range(2):
            db.session.add(Task(id=f't-{i}', name='Sysprep Workflow', status='SUCCESS'))
        db.session.commit()
        with pytest.raises(ValidationError, match='Daily provisioning limit'):
            check_daily_quota(extra_items=1)
        # Still OK to request zero more.
        check_daily_quota(extra_items=0)


def test_evaluate_storage_usage(app):
    with app.app_context():
        app.config['STORAGE_WARN_PCT'] = 65
        app.config['STORAGE_BLOCK_PCT'] = 80
        assert evaluate_storage_usage(10)[0] == 'ok'
        assert evaluate_storage_usage(65)[0] == 'warn'
        assert evaluate_storage_usage(80)[0] == 'block'


def test_check_storage_blocks(app):
    with app.app_context():
        app.config['STORAGE_WARN_PCT'] = 65
        app.config['STORAGE_BLOCK_PCT'] = 80

        def usage(_vmid):
            return {'storage': 'local-lvm', 'node': 'pve', 'used_pct': 85.0, 'used': 1, 'total': 1, 'avail': 0}

        with pytest.raises(ValidationError, match='block'):
            check_storage_for_template(127, get_usage=usage)


def test_tags_windowsserver2019_and_windows11():
    assert pm._tags_indicate_server_2019('windowsserver2019') is True
    assert pm._tags_indicate_windows11('windows11') is True
    assert pm.classify_windows_guest_family.__name__ == 'classify_windows_guest_family'


def test_classify_from_tags(monkeypatch):
    monkeypatch.setattr(
        pm,
        '_template_name_tags',
        lambda vmid, node=None, proxmox=None: (
            'WInServer2019Template' if str(vmid) == '120' else 'Win11-templ2',
            'windowsserver2019' if str(vmid) == '120' else 'windows11',
            'win10' if str(vmid) == '120' else 'win11',
        ),
    )
    assert pm.classify_windows_guest_family(120) == 'server'
    assert pm.classify_windows_guest_family(127) == 'win11'


def test_api_provision_limits(client, app, monkeypatch):
    app.config['API_TOKENS'] = frozenset({'tok'})
    monkeypatch.setattr('app.routes.classify_windows_guest_family', lambda vmid, **kw: 'win11')
    monkeypatch.setattr(
        'app.routes.provision_limits_snapshot',
        lambda family='win11', template_vmid=None, get_usage=None: {
            'family': family,
            'max_cores': 8,
            'max_ram_mb': 65536,
            'max_disk_gb': 600,
            'bulk_max_items': 10,
            'bulk_allowed': family == 'win11',
            'daily_max': 20,
            'daily_used': 0,
            'daily_remaining': 20,
            'batch_remaining': 10,
            'inflight_global': 0,
            'inflight_global_max': 10,
            'storage': {'level': 'ok', 'message': '', 'used_pct': 10},
            'admin_hint': 'hint',
        },
    )
    resp = client.get('/api/provision_limits?template_vmid=127', headers={'Authorization': 'Bearer tok'})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['max_cores'] == 8
    assert body['batch_remaining'] == 10


def test_bulk_rejects_over_batch_max(client, app, monkeypatch):
    app.config['API_TOKENS'] = frozenset({'tok'})
    app.config['BULK_MAX_ITEMS'] = 10
    app.config['BULK_MAX_CONCURRENT_GLOBAL'] = 500
    app.config['PROVISION_MAX_PER_DAY'] = 100
    monkeypatch.setattr('app.routes.require_sysprep_template', lambda vmid, **kw: 'win11')
    monkeypatch.setattr(
        'app.routes._admit_resource_and_quota',
        lambda *a, **k: (True, [], 'win11', {}),
    )
    payload = {
        'shared': {
            'template_vmid': 127,
            'cores': 2,
            'ram': 4096,
            'bridge': 'vmbr0',
            'network_mode': 'dhcp',
            'administrator_password': 'password123',
            'timezone': 'UTC',
            'join_domain': False,
        },
        'items': [{'hostname': f'VDI-{i:03d}'} for i in range(11)],
    }
    resp = client.post('/start_sysprep_bulk_workflow', json=payload, headers={'Authorization': 'Bearer tok'})
    assert resp.status_code == 400
    assert 'max items' in resp.get_json()['error'].lower()


def test_bulk_rejects_server_template(client, app, monkeypatch):
    app.config['API_TOKENS'] = frozenset({'tok'})
    app.config['BULK_MAX_ITEMS'] = 10
    app.config['BULK_MAX_CONCURRENT_GLOBAL'] = 500
    app.config['BULK_MAX_CONCURRENT_PER_REMOTE'] = 500
    app.config['BULK_MAX_INFLIGHT_BATCHES'] = 100
    app.config['PROVISION_MAX_PER_DAY'] = 100
    from contextlib import contextmanager

    @contextmanager
    def _noop_override(_override):
        yield

    monkeypatch.setattr('app.routes.use_pve_override', _noop_override)
    monkeypatch.setattr('app.remotes.attach_pve_override', lambda data: None)
    monkeypatch.setattr('app.routes.require_sysprep_template', lambda vmid, **kw: 'win10')
    monkeypatch.setattr('app.routes.classify_windows_guest_family', lambda vmid, **kw: 'server')

    payload = {
        'shared': {
            'template_vmid': 120,
            'cores': 2,
            'ram': 4096,
            'bridge': 'vmbr0',
            'network_mode': 'dhcp',
            'administrator_password': 'password123',
            'timezone': 'UTC',
            'join_domain': False,
        },
        'items': [{'hostname': 'SRV-001'}],
    }
    resp = client.post('/start_sysprep_bulk_workflow', json=payload, headers={'Authorization': 'Bearer tok'})
    assert resp.status_code == 400
    assert 'windows 11' in resp.get_json()['error'].lower()
