"""Mocked end-to-end smoke for linux_cloudinit_workflow_task (CI-safe)."""
from __future__ import annotations

import contextlib
import uuid

import pytest

from app import db
from app.celery_app import linux_cloudinit_workflow_task
from app.models import Task

pytestmark = pytest.mark.api


def _workflow_data(**overrides):
    data = {
        'template_vmid': 137,
        'hostname': 'linux-smoke',
        'cores': 2,
        'ram': 4096,
        'bridge': 'vmbr0',
        'network_mode': 'static',
        'ip_address': '192.168.123.200',
        'netmask': 24,
        'gateway': '192.168.123.1',
        'ciuser': 'ubuntu',
        'manage_disks': False,
        'os_family': 'linux',
        'fast_waits': True,
    }
    data.update(overrides)
    return data


def _patch_linux_side_effects(monkeypatch, *, clone_vmid=9137):
    import app.celery_app as ca

    calls = {'clone': 0, 'cloudinit': 0, 'power_on': [], 'verify': 0}

    def _require_linux(vmid, **kwargs):
        return 'l26'

    def _clone(*args, **kwargs):
        calls['clone'] += 1
        assert kwargs.get('os_family') == 'linux'
        return {'vmid': clone_vmid, 'uuid': str(uuid.uuid4())}

    def _cloudinit(vmid, **kwargs):
        calls['cloudinit'] += 1
        assert vmid == clone_vmid
        assert kwargs.get('nics')

    def _power_on(vmid):
        calls['power_on'].append(vmid)

    def _wait_agent(vmid, timeout=900, stable_for=20, on_progress=None, **kwargs):
        if on_progress:
            on_progress('guest agent mock ok')
        return True

    def _verify(vmid, data, timeout=600, on_progress=None):
        calls['verify'] += 1
        if on_progress:
            on_progress('verify mock ok')
        return ('hostname linux-smoke; static 192.168.123.200', True)

    def _noop_tag(*args, **kwargs):
        return True, 'ok'

    def _apply_async(*_a, **kwargs):
        args = kwargs.get('args') or ()
        return ca.linux_verify_task(*args)

    monkeypatch.setattr(ca, 'require_linux_guest', _require_linux)
    monkeypatch.setattr(ca, 'clone_vm', _clone)
    monkeypatch.setattr(ca, 'apply_cloudinit_config', _cloudinit)
    monkeypatch.setattr(ca, 'set_lifecycle_tag', _noop_tag)
    monkeypatch.setattr(ca, 'mark_vm_customization_failed', _noop_tag)
    monkeypatch.setattr(ca, 'power_off_vm', lambda *a, **k: None)
    monkeypatch.setattr(ca, 'power_on_vm', _power_on)
    monkeypatch.setattr(ca.time, 'sleep', lambda _s: None)
    monkeypatch.setattr(ca, 'wait_for_guest_agent', _wait_agent)
    monkeypatch.setattr(ca, 'verify_linux_result', _verify)
    monkeypatch.setattr(
        ca,
        'freeze_linux_cloudinit',
        lambda *a, **k: {'detached_drives': ['ide0'], 'cleared': ['ide0']},
    )
    monkeypatch.setattr(ca.linux_verify_task, 'apply_async', _apply_async)
    monkeypatch.setattr(
        'app.provision_limits.check_storage_for_template',
        lambda *a, **k: ('ok', {'level': 'ok'}),
    )
    return calls


def test_linux_cloudinit_workflow_success(client, monkeypatch, app):
    calls = _patch_linux_side_effects(monkeypatch)
    task_id = str(uuid.uuid4())
    with app.app_context():
        db.session.add(Task(
            id=task_id,
            name='Linux Cloud-Init Workflow',
            description='test',
            hostname='linux-smoke',
            template_vmid=137,
        ))
        db.session.commit()

    linux_cloudinit_workflow_task(task_id, _workflow_data())

    with app.app_context():
        task = Task.query.get(task_id)
        assert task.status == 'SUCCESS'
        assert task.progress == 100
        assert task.result_vmid == 9137
    assert calls['clone'] == 1
    assert calls['cloudinit'] == 1
    assert calls['power_on'] == [9137]
    assert calls['verify'] == 1


def test_linux_workflow_dhcp(client, monkeypatch, app):
    """DHCP mode reaches SUCCESS without static IP fields."""
    calls = _patch_linux_side_effects(monkeypatch)
    task_id = str(uuid.uuid4())
    with app.app_context():
        db.session.add(Task(
            id=task_id,
            name='Linux Cloud-Init Workflow',
            description='dhcp test',
            hostname='dhcp-smoke',
            template_vmid=137,
        ))
        db.session.commit()

    linux_cloudinit_workflow_task(
        task_id,
        _workflow_data(
            hostname='dhcp-smoke',
            network_mode='dhcp',
            ip_address='',
            netmask='',
            gateway='',
        ),
    )

    with app.app_context():
        task = Task.query.get(task_id)
        assert task.status == 'SUCCESS'
    assert calls['clone'] == 1


def test_linux_workflow_detach_cloudinit(client, monkeypatch, app):
    """detach_cloudinit_after_ready=True triggers freeze_linux_cloudinit."""
    import app.celery_app as ca
    freeze_calls = []
    orig_patch = _patch_linux_side_effects(monkeypatch)
    monkeypatch.setattr(
        ca,
        'freeze_linux_cloudinit',
        lambda *a, **k: freeze_calls.append(1) or {'detached_drives': ['ide0'], 'cleared': ['ide0']},
    )

    task_id = str(uuid.uuid4())
    with app.app_context():
        db.session.add(Task(
            id=task_id,
            name='Linux Cloud-Init Workflow',
            description='detach test',
            hostname='detach-smoke',
            template_vmid=137,
        ))
        db.session.commit()

    linux_cloudinit_workflow_task(
        task_id,
        _workflow_data(hostname='detach-smoke', detach_cloudinit_after_ready=True),
    )

    with app.app_context():
        task = Task.query.get(task_id)
        assert task.status == 'SUCCESS'
    assert len(freeze_calls) == 1


def test_linux_workflow_os_disk_resize(client, monkeypatch, app):
    """os_disk_gb triggers manage_disks + reconcile_linux_vm_disks."""
    import app.celery_app as ca
    reconcile_calls = []

    calls = _patch_linux_side_effects(monkeypatch)
    monkeypatch.setattr(
        ca,
        'reconcile_linux_vm_disks',
        lambda vmid, disks: reconcile_calls.append((vmid, disks)) or [],
    )

    task_id = str(uuid.uuid4())
    with app.app_context():
        db.session.add(Task(
            id=task_id,
            name='Linux Cloud-Init Workflow',
            description='disk test',
            hostname='disk-smoke',
            template_vmid=137,
        ))
        db.session.commit()

    linux_cloudinit_workflow_task(
        task_id,
        _workflow_data(
            hostname='disk-smoke',
            manage_disks=True,
            os_disk_gb=50,
            disks=[{'role': 'os', 'grow_to_gb': 50}],
        ),
    )

    with app.app_context():
        task = Task.query.get(task_id)
        assert task.status == 'SUCCESS'
    assert len(reconcile_calls) == 1
    assert reconcile_calls[0][0] == 9137
    assert reconcile_calls[0][1][0]['grow_to_gb'] == 50


def test_linux_workflow_multi_nic(client, monkeypatch, app):
    """Multi-NIC payload passes correct nics[] to apply_cloudinit_config."""
    captured_nics = []

    def _cloudinit_capture(vmid, **kwargs):
        captured_nics.append(kwargs.get('nics'))

    import app.celery_app as ca
    calls = _patch_linux_side_effects(monkeypatch)
    monkeypatch.setattr(ca, 'apply_cloudinit_config', _cloudinit_capture)

    task_id = str(uuid.uuid4())
    with app.app_context():
        db.session.add(Task(
            id=task_id,
            name='Linux Cloud-Init Workflow',
            description='multi-nic test',
            hostname='mnic-smoke',
            template_vmid=137,
        ))
        db.session.commit()

    nics = [
        {
            'bridge': 'vmbr0',
            'network_mode': 'static',
            'ip_address': '192.168.123.200',
            'netmask': 24,
            'gateway': '192.168.123.1',
        },
        {
            'bridge': 'vmbr1',
            'network_mode': 'dhcp',
        },
    ]
    linux_cloudinit_workflow_task(
        task_id,
        _workflow_data(hostname='mnic-smoke', nics=nics),
    )

    with app.app_context():
        task = Task.query.get(task_id)
        assert task.status == 'SUCCESS'
    assert len(captured_nics) == 1
    assert len(captured_nics[0]) == 2


def test_linux_workflow_clone_failure(client, monkeypatch, app):
    """Clone failure sets task to FAILURE."""
    import app.celery_app as ca

    calls = _patch_linux_side_effects(monkeypatch)
    monkeypatch.setattr(
        ca, 'clone_vm', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('clone boom')),
    )

    task_id = str(uuid.uuid4())
    with app.app_context():
        db.session.add(Task(
            id=task_id,
            name='Linux Cloud-Init Workflow',
            description='fail test',
            hostname='fail-smoke',
            template_vmid=137,
        ))
        db.session.commit()

    linux_cloudinit_workflow_task(task_id, _workflow_data(hostname='fail-smoke'))

    with app.app_context():
        task = Task.query.get(task_id)
        assert task.status == 'FAILURE'


def test_linux_start_api_requires_auth(client):
    resp = client.post('/start_linux_cloudinit_workflow', json=_workflow_data())
    assert resp.status_code == 401


def test_linux_start_api_ok(client, app, monkeypatch):
    app.config['API_TOKENS'] = frozenset({'linux-token'})
    captured = {}

    class _Task:
        @staticmethod
        def apply_async(args=None, queue=None, **kwargs):
            captured['task_id'] = args[0]
            captured['data'] = args[1]
            captured['queue'] = queue

    monkeypatch.setattr('app.routes.linux_cloudinit_workflow_task', _Task)
    monkeypatch.setattr('app.routes.require_linux_template', lambda vmid: 'l26')
    monkeypatch.setattr(
        'app.routes.check_storage_for_template',
        lambda *a, **k: ('ok', {'level': 'ok'}),
    )
    monkeypatch.setattr('app.routes.check_daily_quota', lambda **k: None)
    monkeypatch.setattr('app.routes.stash_task_secrets', lambda *a, **k: None)
    monkeypatch.setattr('app.routes.use_pve_override', contextlib.nullcontext)
    monkeypatch.setattr('app.remotes.attach_pve_override', lambda data: None)
    monkeypatch.setattr('app.routes.resolve_pve_remote', lambda data, err_fn: (True, None))

    resp = client.post(
        '/start_linux_cloudinit_workflow',
        json=_workflow_data(),
        headers={'Authorization': 'Bearer linux-token'},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body.get('task_id')
    assert captured.get('queue') == 'clone_queue'
    assert captured.get('data', {}).get('hostname') == 'linux-smoke'
