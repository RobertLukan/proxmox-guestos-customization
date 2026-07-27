"""Celery tasks for GuestOS Sysprep workflows."""
from app import celery, app, db
from app.models import Task, _utcnow
from app.proxmox import (
    clone_vm,
    power_on_vm,
    wait_for_guest_agent,
    run_shutdown_command_in_guest,
    get_primary_mac_address,
    use_pve_override,
    require_windows_guest,
    is_windows_server_2019_template,
    reconcile_vm_disks,
)
from app.disks import prepare_disk_plan
from app.remotes import attach_pve_override
from app.util import as_bool as _as_bool
from app.validators import ValidationError, validate_mac
from app.task_progress import update_task_progress
from app.sysprep_render import (
    _validate_sysprep_network,
    _prepare_domain_join,
    _render_sysprep_files,
    _write_sysprep_files,
)
from app.sysprep_verify import (
    _parse_domain_membership,
    _domains_match,
    _read_domain_membership,
    _guest_setup_marker,
    _verify_sysprep_result,
    _verify_disks,
)
from app.sysprep_power import (
    _guest_agent_responsive,
    _wait_for_vm_stopped,
    _wait_for_sysprep_shutdown,
    _complete_sysprep_power_cycle,
)
import base64
import json
import time


# Re-export helpers so existing tests can monkeypatch ``app.celery_app.*``.
__all__ = [
    'update_task_progress',
    'sysprep_workflow_task',
    'sysprep_existing_vm_task',
    '_verify_sysprep_result',
    '_verify_disks',
    '_parse_domain_membership',
    '_domains_match',
    '_read_domain_membership',
    '_guest_setup_marker',
    '_complete_sysprep_power_cycle',
    '_wait_for_sysprep_shutdown',
    '_wait_for_vm_stopped',
    '_guest_agent_responsive',
    '_validate_sysprep_network',
    '_prepare_domain_join',
    '_render_sysprep_files',
    '_write_sysprep_files',
    '_sysprep_wait_timings',
]


# Production defaults for the fixed first-boot sleep and guest-agent stability.
# Smoke tests may pass fast_waits=true to use the short lab timings instead.
_BOOT_SETTLE_DEFAULT = 180
_AGENT_STABLE_DEFAULT = 60
_BOOT_SETTLE_FAST = 30
_AGENT_STABLE_FAST = 15


def _sysprep_wait_timings(data=None):
    """Return (boot_settle_seconds, agent_stable_seconds).

    Priority: request ``fast_waits`` → app config env overrides → production defaults.
    """
    data = data or {}
    if _as_bool(data.get('fast_waits')):
        return _BOOT_SETTLE_FAST, _AGENT_STABLE_FAST
    boot = app.config.get('SYSPREP_BOOT_SETTLE_SECONDS', _BOOT_SETTLE_DEFAULT)
    stable = app.config.get('SYSPREP_AGENT_STABLE_SECONDS', _AGENT_STABLE_DEFAULT)
    try:
        boot = max(0, int(boot))
    except (TypeError, ValueError):
        boot = _BOOT_SETTLE_DEFAULT
    try:
        stable = max(0, int(stable))
    except (TypeError, ValueError):
        stable = _AGENT_STABLE_DEFAULT
    return boot, stable


@celery.task(bind=True)
def sysprep_workflow_task(self, task_id, data):
    with app.app_context():
        try:
            pve_override = attach_pve_override(data)
        except ValueError as e:
            task = Task.query.get(task_id)
            if task:
                task.status = 'FAILURE'
                task.message = str(e)
                db.session.commit()
            return
        with use_pve_override(pve_override):
            try:
                # 0. Validate user-supplied network + domain values before templating.
                try:
                    require_windows_guest(data['template_vmid'])
                    _validate_sysprep_network(data)
                    _prepare_domain_join(data)
                    if _as_bool(data.get('manage_disks')) and not is_windows_server_2019_template(
                        data['template_vmid']
                    ):
                        raise ValidationError(
                            'manage_disks is only supported for Windows Server 2019 '
                            'templates (name/tag must include server2019, win2019, '
                            'ws2019, or guestos-disk). Win11 and other guests keep a '
                            'flat disk layout — omit manage_disks or retarget a '
                            'Server 2019 template.'
                        )
                    prepare_disk_plan(data)
                except ValidationError as e:
                    task = Task.query.get(task_id)
                    task.status = 'FAILURE'
                    task.message = f"Invalid sysprep input: {e}"
                    db.session.commit()
                    return
                except ValueError as e:
                    task = Task.query.get(task_id)
                    task.status = 'FAILURE'
                    task.message = str(e)
                    db.session.commit()
                    return

                # 1. Clone the VM
                update_task_progress(task_id, 10, "Cloning VM...")
                clone_result = clone_vm(
                    data['template_vmid'],
                    data['hostname'],
                    data['cores'],
                    data['ram'],
                    data['bridge'],
                    data.get('vlan')  # Use .get() for the optional vlan
                )
                new_vmid = clone_result['vmid']
                update_task_progress(
                    task_id,
                    25,
                    f"VM cloned successfully. New VMID: {new_vmid}",
                    result_vmid=new_vmid,
                )

                # 1b. Optional disk reconcile (attach / grow) before first boot.
                if data.get('manage_disks'):
                    update_task_progress(task_id, 30, "Reconciling disks on Proxmox...")
                    guest_plan = reconcile_vm_disks(new_vmid, data['disks'])
                    data['disk_guest_plan'] = guest_plan
                    data['disk_plan_b64'] = base64.b64encode(
                        json.dumps(guest_plan).encode('utf-8')
                    ).decode('ascii')
                else:
                    data['disk_guest_plan'] = []
                    data['disk_plan_b64'] = ''

                # 2. Resolve the primary NIC MAC (for robust adapter selection) and
                #    render the answer file + post-setup scripts.
                update_task_progress(task_id, 35, "Generating sysprep files...")
                mac = get_primary_mac_address(new_vmid)
                if mac:
                    data['primary_mac_address'] = validate_mac(mac)
                unattended_xml, setup_ps1, setup_complete = _render_sysprep_files(data)

                # 3. Power on the VM
                update_task_progress(task_id, 50, "Powering on VM...")
                power_on_vm(new_vmid)
                # Win11 (and some Server builds) reboot several times before the
                # guest agent stays up; give the first boot cycle room to settle.
                # Smoke/lab may pass fast_waits or set SYSPREP_* env overrides.
                boot_settle, agent_stable = _sysprep_wait_timings(data)
                if boot_settle:
                    update_task_progress(
                        task_id,
                        55,
                        f"Waiting {boot_settle}s for initial OS reboots...",
                    )
                    time.sleep(boot_settle)

                # 4. Wait for a *stable* QEMU Guest Agent (not just the first ping).
                update_task_progress(task_id, 60, "Waiting for QEMU Guest Agent to stabilize...")
                wait_for_guest_agent(new_vmid, timeout=1200, stable_for=agent_stable)
                update_task_progress(task_id, 70, "QEMU Guest Agent is ready.")

                # 5. Write the answer file + post-setup scripts to the guest.
                update_task_progress(task_id, 80, "Writing sysprep files to guest...")
                _write_sysprep_files(new_vmid, unattended_xml, setup_ps1, setup_complete)
                update_task_progress(task_id, 85, "Sysprep files written successfully.")

                # 6. Run Sysprep
                update_task_progress(task_id, 88, "Running Sysprep...")
                sysprep_command = (
                    r'cmd.exe /c "C:\Windows\System32\Sysprep\sysprep.exe '
                    r'/generalize /oobe /shutdown '
                    r'/unattend:C:\Windows\System32\Sysprep\unattended.xml"'
                )
                run_shutdown_command_in_guest(new_vmid, sysprep_command)

                # 7. Verify: wait for shutdown or post-sysprep boot, then confirm
                # the guest agent before reporting success.
                if not _complete_sysprep_power_cycle(
                    task_id, new_vmid, progress_base=92, agent_stable_for=agent_stable
                ):
                    task = Task.query.get(task_id)
                    task.status = 'FAILURE'
                    task.message = (
                        "Timed out waiting for the VM to shut down (or reboot) after Sysprep."
                    )
                    db.session.commit()
                    return

                update_task_progress(task_id, 98, "Verifying hostname and network via guest agent...")
                verify_summary, verify_ok = _verify_sysprep_result(
                    new_vmid,
                    data.get('hostname'),
                    expected_ip=None if data.get('use_dhcp') else data.get('ip_address'),
                    expected_domain=data.get('domain_name') if data.get('join_domain') else None,
                    on_progress=lambda msg: update_task_progress(task_id, 98, msg),
                )
                if data.get('manage_disks') and data.get('disk_guest_plan'):
                    disk_summary, disk_ok = _verify_disks(
                        new_vmid,
                        data['disk_guest_plan'],
                        on_progress=lambda msg: update_task_progress(task_id, 98, msg),
                    )
                    verify_summary = f'{verify_summary}; {disk_summary}'
                    verify_ok = verify_ok and disk_ok

                task = Task.query.get(task_id)
                if verify_ok:
                    task.status = 'SUCCESS'
                    task.progress = 100
                    task.message = (
                        f"Sysprep workflow for {data['hostname']} completed. "
                        f"Verify: {verify_summary}"
                    )
                else:
                    task.status = 'FAILURE'
                    task.progress = 100
                    task.message = (
                        f"Sysprep finished but verification failed for {data['hostname']}: "
                        f"{verify_summary}"
                    )
                db.session.commit()

            except Exception as e:
                app.logger.error(f"Task {task_id} failed: {e}", exc_info=True)
                task = Task.query.get(task_id)
                task.status = 'FAILURE'
                task.message = f"An error occurred: {e}"
                db.session.commit()


@celery.task(bind=True)
def sysprep_existing_vm_task(self, task_id, data):
    """Disabled: in-place Sysprep of existing VMs is not supported.

    Kept as a Celery stub so old workers/queue messages fail cleanly instead of
    running generalize against production guests.
    """
    with app.app_context():
        task = Task.query.get(task_id)
        if not task:
            return
        task.status = 'FAILURE'
        task.progress = 100
        task.message = (
            'In-place Sysprep is disabled. Use Clone + Sysprep from a Windows template.'
        )
        task.updated_at = _utcnow()
        db.session.commit()
