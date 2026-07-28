"""Mocked end-to-end smoke for sysprep_workflow_task (CI-safe).

Walks the full Celery task with Proxmox/guest-agent side effects stubbed out.
No live PVE or Windows guests required.
"""
from __future__ import annotations

import uuid

import pytest

from app import db
from app.celery_app import sysprep_workflow_task
from app.models import Task

pytestmark = pytest.mark.api


def _workflow_data(**overrides):
    data = {
        'template_vmid': 120,
        'hostname': 'SMOKE01',
        'cores': 2,
        'ram': 4096,
        'bridge': 'vmbr0',
        'network_mode': 'dhcp',
        'administrator_password': 'ChangeMe123!',
        'timezone': 'UTC',
        'join_domain': False,
        'manage_disks': False,
    }
    data.update(overrides)
    return data


def _patch_workflow_side_effects(monkeypatch, *, clone_vmid=9055, fail_clone=False):
    """Stub every external dependency used by sysprep_workflow_task."""
    import app.celery_app as ca

    calls = {
        'clone': 0,
        'power_on': [],
        'write_files': 0,
        'sysprep_cmd': 0,
        'sleeps': 0,
    }

    def _require_windows(vmid, **kwargs):
        return 'win11'

    def _clone(*args, **kwargs):
        calls['clone'] += 1
        if fail_clone:
            raise Exception('clone failed (simulated)')
        return {'vmid': clone_vmid, 'uuid': str(uuid.uuid4())}

    def _mac(vmid):
        return 'BC:24:11:00:11:22'

    def _power_on(vmid):
        calls['power_on'].append(vmid)

    def _sleep(_seconds):
        calls['sleeps'] += 1

    def _wait_agent(vmid, timeout=1200, stable_for=60):
        return True

    def _write_files(vmid, unattended_xml, setup_ps1, setup_complete):
        calls['write_files'] += 1
        assert unattended_xml
        assert setup_ps1
        assert setup_complete

    def _run_shutdown(vmid, command):
        calls['sysprep_cmd'] += 1
        assert 'sysprep.exe' in command.lower()

    def _power_cycle(task_id, vmid, progress_base=92, agent_stable_for=60):
        return True

    def _verify(
        vmid,
        expected_hostname,
        expected_ip=None,
        expected_domain=None,
        expected_ipv6=None,
        on_progress=None,
        **kwargs,
    ):
        if on_progress:
            on_progress('verify mock ok')
        return ('hostname=SMOKE01 ip=dhcp domain=-', True)

    def _noop_tag(*args, **kwargs):
        return True, 'ok'

    def _macs(vmid):
        return ['BC:24:11:00:11:22']

    monkeypatch.setattr(ca, 'require_windows_guest', _require_windows)
    monkeypatch.setattr(ca, 'is_windows_server_2019_template', lambda vmid: True)
    monkeypatch.setattr(ca, 'clone_vm', _clone)
    monkeypatch.setattr(ca, 'get_primary_mac_address', _mac)
    monkeypatch.setattr(ca, 'get_vm_nic_macs', _macs)
    monkeypatch.setattr(ca, 'set_lifecycle_tag', _noop_tag)
    monkeypatch.setattr(ca, 'mark_vm_customization_failed', _noop_tag)
    monkeypatch.setattr(ca, 'power_on_vm', _power_on)
    monkeypatch.setattr(ca.time, 'sleep', _sleep)
    monkeypatch.setattr(ca, 'wait_for_guest_agent', _wait_agent)
    monkeypatch.setattr(ca, '_write_sysprep_files', _write_files)
    monkeypatch.setattr(ca, 'run_shutdown_command_in_guest', _run_shutdown)
    monkeypatch.setattr(ca, '_complete_sysprep_power_cycle', _power_cycle)
    monkeypatch.setattr(ca, '_verify_sysprep_result', _verify)
    monkeypatch.setattr(
        ca.sysprep_verify_task,
        'apply_async',
        lambda args=None, queue=None, **kwargs: ca.sysprep_verify_task.run(*args),
    )
    monkeypatch.setattr(
        ca,
        'reconcile_vm_disks',
        lambda vmid, plan: (_ for _ in ()).throw(AssertionError('reconcile should not run')),
    )
    return calls


def test_sysprep_workflow_manage_disks_reconciles(app, monkeypatch):
    calls = _patch_workflow_side_effects(monkeypatch, clone_vmid=9060)
    import app.celery_app as ca

    reconciled = {}

    def _reconcile(vmid, plan):
        reconciled['vmid'] = vmid
        reconciled['plan'] = plan
        return [
            {
                'role': 'os',
                'serial': 'guestos-os',
                'drive_letter': 'C',
                'min_size_gb': 40,
                'ensure_pagefile': False,
                'reformat': False,
                'extend': False,
                'pve_key': 'scsi0',
            },
            {
                'role': 'pagefile',
                'serial': 'guestos-pagefile',
                'drive_letter': 'P',
                'min_size_gb': 16,
                'ensure_pagefile': True,
                'reformat': False,
                'extend': True,
                'pve_key': 'scsi1',
            },
        ]

    monkeypatch.setattr(ca, 'reconcile_vm_disks', _reconcile)
    monkeypatch.setattr(ca, '_verify_disks', lambda *a, **k: ('disks: pagefile=P: 16G in use', True))

    task_id = str(uuid.uuid4())
    with app.app_context():
        db.session.add(Task(id=task_id, name='Sysprep Workflow', description='disks'))
        db.session.commit()
        sysprep_workflow_task.run(
            task_id,
            _workflow_data(
                manage_disks=True,
                disks=[
                    {'role': 'os'},
                    {'role': 'pagefile', 'size_gb': 16, 'drive_letter': 'P', 'ensure_pagefile': True},
                ],
            ),
        )
        task = db.session.get(Task, task_id)
        assert task.status == 'SUCCESS'
        assert reconciled.get('vmid') == 9060
        assert any(d['role'] == 'pagefile' for d in reconciled.get('plan') or [])
        assert 'disks:' in (task.message or '')


def test_sysprep_workflow_mocked_success(app, monkeypatch):
    calls = _patch_workflow_side_effects(monkeypatch, clone_vmid=9055)
    task_id = str(uuid.uuid4())
    with app.app_context():
        db.session.add(Task(id=task_id, name='Sysprep Workflow', description='smoke'))
        db.session.commit()

        sysprep_workflow_task.run(task_id, _workflow_data())

        task = db.session.get(Task, task_id)
        assert task is not None
        assert task.status == 'SUCCESS'
        assert task.progress == 100
        assert task.result_vmid == 9055
        assert 'SMOKE01' in (task.message or '')
        assert calls['clone'] == 1
        assert calls['write_files'] == 1
        assert calls['sysprep_cmd'] == 1
        assert 9055 in calls['power_on']
        assert calls['sleeps'] >= 1  # initial boot-settle wait (mocked)


def test_sysprep_workflow_rejects_manage_disks_on_non_server_2019(app, monkeypatch):
    _patch_workflow_side_effects(monkeypatch, clone_vmid=9057)
    import app.celery_app as ca

    monkeypatch.setattr(ca, 'is_windows_server_2019_template', lambda vmid: False)

    task_id = str(uuid.uuid4())
    with app.app_context():
        db.session.add(Task(id=task_id, name='Sysprep Workflow', description='non-srv2019'))
        db.session.commit()
        sysprep_workflow_task.run(
            task_id,
            _workflow_data(
                manage_disks=True,
                disks=[
                    {'role': 'os'},
                    {'role': 'pagefile', 'size_gb': 16, 'drive_letter': 'P', 'ensure_pagefile': True},
                ],
            ),
        )
        task = db.session.get(Task, task_id)
        assert task.status == 'FAILURE'
        assert 'manage_disks requires a Windows Server 2019' in (task.message or '') or \
        'manage_disks is only supported' in (task.message or '')


def test_sysprep_workflow_mocked_clone_failure(app, monkeypatch):
    _patch_workflow_side_effects(monkeypatch, fail_clone=True)
    task_id = str(uuid.uuid4())
    with app.app_context():
        db.session.add(Task(id=task_id, name='Sysprep Workflow', description='smoke-fail'))
        db.session.commit()

        sysprep_workflow_task.run(task_id, _workflow_data())

        task = db.session.get(Task, task_id)
        assert task.status == 'FAILURE'
        assert 'clone failed' in (task.message or '').lower()


def test_sysprep_workflow_api_enqueues_then_mocked_task_succeeds(client, app, monkeypatch):
    """HTTP start + run the real task body with mocks (closest to PDM machine API)."""
    app.config['API_TOKENS'] = frozenset({'smoke-token'})
    monkeypatch.setattr('app.routes.require_sysprep_template', lambda vmid, **kw: 'win11')
    monkeypatch.setattr('app.routes.require_windows_guest', lambda vmid, **kw: 'win11')

    captured = {}

    class _Task:
        @staticmethod
        def apply_async(args=None, queue=None, **kwargs):
            task_id, data = args
            captured['task_id'] = task_id
            captured['data'] = data

    monkeypatch.setattr('app.routes.sysprep_workflow_task', _Task)

    resp = client.post(
        '/start_sysprep_workflow',
        json=_workflow_data(template_vmid=120),
        headers={'Authorization': 'Bearer smoke-token'},
    )
    assert resp.status_code == 200
    task_id = resp.get_json()['task_id']
    assert captured['task_id'] == task_id

    # Now execute the real Celery task against the same Task row.
    _patch_workflow_side_effects(monkeypatch, clone_vmid=9060)
    with app.app_context():
        sysprep_workflow_task.run(task_id, captured['data'])
        task = db.session.get(Task, task_id)
        assert task.status == 'SUCCESS'
        assert task.result_vmid == 9060
