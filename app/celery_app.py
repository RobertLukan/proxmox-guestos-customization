"""Celery tasks for GuestOS Sysprep workflows."""
from app import celery, app, db
from app.models import Task, _utcnow
from app.proxmox import (
    clone_vm,
    power_on_vm,
    wait_for_guest_agent,
    run_shutdown_command_in_guest,
    get_primary_mac_address,
    get_vm_nic_macs,
    use_pve_override,
    require_windows_guest,
    is_windows_server_template,
    reconcile_vm_disks,
    set_lifecycle_tag,
    mark_vm_customization_failed,
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
    'sysprep_verify_task',
    '_fail_sysprep_task',
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
    boot = app.config.get('SYSPREP_BOOT_SETTLE_SECONDS')
    agent = app.config.get('SYSPREP_AGENT_STABLE_SECONDS')
    try:
        boot_settle = int(boot) if boot is not None else _BOOT_SETTLE_DEFAULT
    except (TypeError, ValueError):
        boot_settle = _BOOT_SETTLE_DEFAULT
    try:
        agent_stable = int(agent) if agent is not None else _AGENT_STABLE_DEFAULT
    except (TypeError, ValueError):
        agent_stable = _AGENT_STABLE_DEFAULT
    return max(0, boot_settle), max(0, agent_stable)


def _task_cancelled(task_id):
    task = Task.query.get(task_id)
    return bool(task and task.status == 'CANCELLED')


def _fail_sysprep_task(task_id, message, vmid=None, hostname=None, error_code=None):
    """Mark task FAILURE and, when a clone exists, rename/tag it for analysis."""
    task = Task.query.get(task_id)
    if not task:
        return
    if task.status == 'CANCELLED':
        return
    task.status = 'FAILURE'
    task.message = message
    if error_code:
        task.error_code = error_code
    if vmid is not None:
        task.result_vmid = vmid
    db.session.commit()
    if vmid:
        try:
            ok, detail = mark_vm_customization_failed(vmid, hostname=hostname or task.hostname)
            app.logger.info("Failed customization mark for VM %s: %s (%s)", vmid, ok, detail)
        except Exception as e:
            app.logger.warning("Could not mark failed VM %s: %s", vmid, e)


def _clone_nics_arg(data):
    """Return nics list for clone_vm (bridge/vlan only), or None for single-NIC path."""
    nics = data.get('nics')
    if not isinstance(nics, list) or len(nics) <= 1:
        return None
    out = []
    for nic in nics:
        out.append({
            'bridge': nic.get('bridge') or data.get('bridge'),
            'vlan': nic.get('vlan') if nic.get('vlan') is not None else data.get('vlan'),
        })
    return out


def _attach_macs_to_nics(data, vmid):
    """Fill primary_mac_address on each nic from the live VM config."""
    macs = get_vm_nic_macs(vmid)
    nics = data.get('nics') or []
    for i, nic in enumerate(nics):
        if i < len(macs) and macs[i]:
            try:
                nic['primary_mac_address'] = validate_mac(macs[i])
            except ValidationError:
                pass
    if nics and nics[0].get('primary_mac_address'):
        data['primary_mac_address'] = nics[0]['primary_mac_address']
    elif not data.get('primary_mac_address'):
        mac = get_primary_mac_address(vmid)
        if mac:
            data['primary_mac_address'] = validate_mac(mac)


@celery.task(bind=True)
def sysprep_workflow_task(self, task_id, data):
    with app.app_context():
        new_vmid = None
        hostname = (data or {}).get('hostname')
        try:
            try:
                pve_override = attach_pve_override(data)
            except ValueError as e:
                _fail_sysprep_task(task_id, str(e), hostname=hostname, error_code='remote')
                return

            with use_pve_override(pve_override):
                try:
                    require_windows_guest(data['template_vmid'])
                    _validate_sysprep_network(data)
                    _prepare_domain_join(data)
                    if _as_bool(data.get('manage_disks')):
                        if not is_windows_server_template(data['template_vmid']):
                            raise ValidationError(
                                'manage_disks requires a Windows Server template '
                                '(name/tag: windowsserver2019|2022|2025, server2022, '
                                'guestos-disk, …). Windows 11 and other guests keep a '
                                'flat disk layout — omit manage_disks or retarget a '
                                'Server template.'
                            )
                        prepare_disk_plan(data)
                        from app.proxmox import classify_windows_guest_family
                        from app.provision_limits import validate_resource_caps, check_storage_for_template
                        family = classify_windows_guest_family(data['template_vmid'])
                        validate_resource_caps(data, family)
                        check_storage_for_template(data['template_vmid'])
                    else:
                        from app.proxmox import classify_windows_guest_family
                        from app.provision_limits import validate_resource_caps, check_storage_for_template
                        family = classify_windows_guest_family(data['template_vmid'])
                        validate_resource_caps(data, family)
                        check_storage_for_template(data['template_vmid'])
                except ValidationError as e:
                    _fail_sysprep_task(
                        task_id, f"Invalid sysprep input: {e}", hostname=hostname, error_code='validation'
                    )
                    return
                except ValueError as e:
                    _fail_sysprep_task(task_id, str(e), hostname=hostname, error_code='validation')
                    return

                if _task_cancelled(task_id):
                    return
                update_task_progress(task_id, 10, "Cloning VM...")
                clone_result = clone_vm(
                    data['template_vmid'],
                    data['hostname'],
                    data['cores'],
                    data['ram'],
                    data['bridge'],
                    data.get('vlan'),
                    nics=_clone_nics_arg(data),
                )
                new_vmid = clone_result['vmid']
                update_task_progress(
                    task_id,
                    25,
                    f"VM cloned successfully. New VMID: {new_vmid}",
                    result_vmid=new_vmid,
                )

                if data.get('manage_disks'):
                    set_lifecycle_tag(new_vmid, 'lifecycle-customizing')
                    update_task_progress(task_id, 30, "Reconciling disks on Proxmox...")
                    guest_plan = reconcile_vm_disks(new_vmid, data['disks'])
                    data['disk_guest_plan'] = guest_plan
                    data['disk_plan_b64'] = base64.b64encode(
                        json.dumps(guest_plan).encode('utf-8')
                    ).decode('ascii')
                else:
                    data['disk_guest_plan'] = []
                    data['disk_plan_b64'] = ''

                if _task_cancelled(task_id):
                    return
                set_lifecycle_tag(new_vmid, 'lifecycle-customizing')
                update_task_progress(task_id, 35, "Generating sysprep files...")
                _attach_macs_to_nics(data, new_vmid)
                # Re-pack nics_b64 now that MACs are known.
                _validate_sysprep_network(data)
                unattended_xml, setup_ps1, setup_complete = _render_sysprep_files(data)

                if _task_cancelled(task_id):
                    return
                set_lifecycle_tag(new_vmid, 'lifecycle-booting')
                update_task_progress(task_id, 50, "Powering on VM...")
                power_on_vm(new_vmid)
                boot_settle, agent_stable = _sysprep_wait_timings(data)
                if boot_settle:
                    update_task_progress(
                        task_id,
                        55,
                        f"Waiting {boot_settle}s for initial OS reboots...",
                    )
                    time.sleep(boot_settle)

                if _task_cancelled(task_id):
                    return
                update_task_progress(task_id, 60, "Waiting for QEMU Guest Agent to stabilize...")
                wait_for_guest_agent(new_vmid, timeout=1200, stable_for=agent_stable)
                update_task_progress(task_id, 70, "QEMU Guest Agent is ready.")

                if _task_cancelled(task_id):
                    return
                set_lifecycle_tag(new_vmid, 'lifecycle-customizing')
                update_task_progress(task_id, 80, "Writing sysprep files to guest...")
                _write_sysprep_files(new_vmid, unattended_xml, setup_ps1, setup_complete)
                update_task_progress(task_id, 85, "Sysprep files written successfully.")

                if _task_cancelled(task_id):
                    return
                set_lifecycle_tag(new_vmid, 'lifecycle-sysprep')
                update_task_progress(task_id, 88, "Running Sysprep...")
                sysprep_command = (
                    r'cmd.exe /c "C:\Windows\System32\Sysprep\sysprep.exe '
                    r'/generalize /oobe /shutdown '
                    r'/unattend:C:\Windows\System32\Sysprep\unattended.xml"'
                )
                run_shutdown_command_in_guest(new_vmid, sysprep_command)

                if not _complete_sysprep_power_cycle(
                    task_id, new_vmid, progress_base=92, agent_stable_for=agent_stable
                ):
                    _fail_sysprep_task(
                        task_id,
                        "Timed out waiting for the VM to shut down (or reboot) after Sysprep.",
                        vmid=new_vmid,
                        hostname=hostname,
                        error_code='sysprep_timeout',
                    )
                    return

                if _task_cancelled(task_id):
                    return
                set_lifecycle_tag(new_vmid, 'lifecycle-verifying')
                update_task_progress(task_id, 95, "Clone and Sysprep phase complete; queued for verification...")
                sysprep_verify_task.apply_async(
                    args=(task_id, new_vmid, data),
                    queue='verify_queue',
                )
                return

        except Exception as e:
            app.logger.error(f"Task {task_id} failed: {e}", exc_info=True)
            _fail_sysprep_task(
                task_id,
                f"An error occurred: {e}",
                vmid=new_vmid,
                hostname=hostname,
                error_code='exception',
            )


@celery.task(bind=True)
def sysprep_verify_task(self, task_id, vmid, data):
    with app.app_context():
        if _task_cancelled(task_id):
            return
        hostname = (data or {}).get('hostname')
        try:
            set_lifecycle_tag(vmid, 'lifecycle-verifying')
            update_task_progress(task_id, 98, "Verifying hostname and network via guest agent...")
            expected_ip = None if data.get('use_dhcp') else data.get('ip_address')
            expected_ipv6 = data.get('ipv6_address') if data.get('enable_ipv6') else None
            verify_summary, verify_ok = _verify_sysprep_result(
                vmid,
                data.get('hostname'),
                expected_ip=expected_ip,
                expected_domain=data.get('domain_name') if data.get('join_domain') else None,
                expected_ipv6=expected_ipv6,
                on_progress=lambda msg: update_task_progress(task_id, 98, msg),
            )
            if data.get('manage_disks') and data.get('disk_guest_plan'):
                disk_summary, disk_ok = _verify_disks(
                    vmid,
                    data['disk_guest_plan'],
                    on_progress=lambda msg: update_task_progress(task_id, 98, msg),
                )
                verify_summary = f'{verify_summary}; {disk_summary}'
                verify_ok = verify_ok and disk_ok

            task = Task.query.get(task_id)
            if not task:
                return
            if task.status == 'CANCELLED':
                return
            if verify_ok:
                set_lifecycle_tag(vmid, 'lifecycle-ready')
                task.status = 'SUCCESS'
                task.progress = 100
                task.message = (
                    f"Sysprep workflow for {data['hostname']} completed. "
                    f"Verify: {verify_summary}"
                )
                db.session.commit()
            else:
                _fail_sysprep_task(
                    task_id,
                    f"Sysprep finished but verification failed for {data['hostname']}: {verify_summary}",
                    vmid=vmid,
                    hostname=hostname,
                    error_code='verify',
                )
        except Exception as e:
            app.logger.error(f"Verify task {task_id} failed: {e}", exc_info=True)
            _fail_sysprep_task(
                task_id,
                f"An error occurred during verify phase: {e}",
                vmid=vmid,
                hostname=hostname,
                error_code='verify_exception',
            )


@celery.task(bind=True)
def sysprep_existing_vm_task(self, task_id, data):
    """Disabled: in-place Sysprep of existing VMs is not supported.

    Kept as a Celery stub so old workers/queue messages fail cleanly instead of
    running generalize against production guests.
    """
    with app.app_context():
        task = Task.query.get(task_id)
        if task:
            task.status = 'FAILURE'
            task.message = (
                'In-place Sysprep of existing VMs is disabled. '
                'Clone from a Windows template instead.'
            )
            task.error_code = 'inplace_disabled'
            db.session.commit()
