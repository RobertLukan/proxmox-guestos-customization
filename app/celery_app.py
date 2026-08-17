"""Celery tasks for GuestOS Sysprep and Linux cloud-init workflows."""
import logging

from celery.exceptions import SoftTimeLimitExceeded

from app import celery, app, db
from app.models import Task, _utcnow
from app.task_janitor import finalize_batch_if_done as _finalize_batch_if_done
from app.proxmox import (
    clone_vm,
    power_on_vm,
    power_off_vm,
    wait_for_guest_agent,
    run_shutdown_command_in_guest,
    get_primary_mac_address,
    get_vm_nic_macs,
    use_pve_override,
    require_windows_guest,
    require_linux_guest,
    is_windows_server_template,
    reconcile_vm_disks,
    apply_cloudinit_config,
    freeze_linux_cloudinit,
    set_lifecycle_tag,
    mark_vm_customization_failed,
)
from app.disks import prepare_disk_plan
from app.linux_cloudinit import prepare_linux_payload, clone_nic_list
from app.linux_disks import reconcile_linux_vm_disks
from app.linux_verify import verify_linux_result
from app.remotes import attach_pve_override
from app.task_secrets import load_task_secrets, scrub_workflow_secrets
from app.util import as_bool as _as_bool
from app.validators import ValidationError, validate_mac
from app.task_progress import (
    append_task_log,
    record_domain_join_method,
    record_host_dc_preflight,
    update_task_progress,
)
from app.sysprep_render import (
    _validate_sysprep_network,
    _prepare_domain_join,
    _render_sysprep_files,
    _write_sysprep_files,
    _ensure_server_product_key,
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
    SysprepGuestFailed,
)
import base64
import json
import time


# Re-export helpers so existing tests can monkeypatch ``app.celery_app.*``.
__all__ = [
    'update_task_progress',
    'sysprep_workflow_task',
    'sysprep_existing_vm_task',
    'linux_cloudinit_workflow_task',
    'linux_verify_task',
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
    'SysprepGuestFailed',
    '_validate_sysprep_network',
    '_prepare_domain_join',
    '_render_sysprep_files',
    '_write_sysprep_files',
    '_ensure_server_product_key',
    '_sysprep_wait_timings',
    '_linux_wait_timings',
    'sysprep_verify_task',
    '_fail_sysprep_task',
    '_abandon_cancelled_clone',
]


# Production defaults for the fixed first-boot sleep and guest-agent stability.
# Request ``fast_waits`` is honored only when TESTING or ALLOW_FAST_WAITS.
_BOOT_SETTLE_DEFAULT = 180
_AGENT_STABLE_DEFAULT = 60
_BOOT_SETTLE_FAST = 30
_AGENT_STABLE_FAST = 15


def _sysprep_wait_timings(data=None):
    """Return (boot_settle_seconds, agent_stable_seconds).

    Priority: gated ``fast_waits`` → app config env overrides → production defaults.
    """
    data = data or {}
    allow_fast = bool(app.config.get('TESTING')) or bool(app.config.get('ALLOW_FAST_WAITS'))
    if _as_bool(data.get('fast_waits')) and allow_fast:
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


_LINUX_BOOT_SETTLE_DEFAULT = 45
_LINUX_AGENT_STABLE_DEFAULT = 20
_LINUX_BOOT_SETTLE_FAST = 5
_LINUX_AGENT_STABLE_FAST = 5


def _linux_wait_timings(data=None):
    """Return (boot_settle_seconds, agent_stable_seconds) for cloud-init clones."""
    data = data or {}
    allow_fast = bool(app.config.get('TESTING')) or bool(app.config.get('ALLOW_FAST_WAITS'))
    if _as_bool(data.get('fast_waits')) and allow_fast:
        return _LINUX_BOOT_SETTLE_FAST, _LINUX_AGENT_STABLE_FAST
    boot = app.config.get('LINUX_BOOT_SETTLE_SECONDS')
    agent = app.config.get('LINUX_AGENT_STABLE_SECONDS')
    try:
        boot_settle = int(boot) if boot is not None else _LINUX_BOOT_SETTLE_DEFAULT
    except (TypeError, ValueError):
        boot_settle = _LINUX_BOOT_SETTLE_DEFAULT
    try:
        agent_stable = int(agent) if agent is not None else _LINUX_AGENT_STABLE_DEFAULT
    except (TypeError, ValueError):
        agent_stable = _LINUX_AGENT_STABLE_DEFAULT
    return max(0, boot_settle), max(0, agent_stable)


def _task_cancelled(task_id):
    task = Task.query.get(task_id)
    return bool(task and task.status == 'CANCELLED')


def _mark_clone_failed(vmid, hostname=None, data=None, stop=False):
    """Rename/tag a clone on the correct PVE remote; optionally hard-stop it."""
    if not vmid:
        return
    try:
        override = attach_pve_override(data or {})
    except ValueError as e:
        app.logger.warning("Could not resolve PVE remote to mark VM %s: %s", vmid, e)
        override = None
    try:
        with use_pve_override(override):
            ok, detail = mark_vm_customization_failed(vmid, hostname=hostname)
            app.logger.info("Failed customization mark for VM %s: %s (%s)", vmid, ok, detail)
            if stop:
                try:
                    power_off_vm(vmid)
                except Exception as e:
                    app.logger.warning("Could not stop cancelled VM %s: %s", vmid, e)
    except Exception as e:
        app.logger.warning("Could not mark failed VM %s: %s", vmid, e)


def _abandon_cancelled_clone(task_id, vmid=None, hostname=None, data=None):
    """Persist result_vmid and tag/stop a clone when the task was cancelled."""
    task = Task.query.get(task_id)
    batch_id = task.batch_id if task else None
    if task:
        if vmid is not None:
            task.result_vmid = vmid
        if task.message and 'cancel' not in (task.message or '').lower():
            task.message = f"{task.message} Clone abandoned after cancel."
        elif not task.message:
            task.message = 'Cancelled; clone abandoned (tagged failed-customization).'
        append_task_log(task, task.message)
        db.session.commit()
    if vmid:
        _mark_clone_failed(vmid, hostname=hostname or (task.hostname if task else None), data=data, stop=True)
    _finalize_batch_if_done(batch_id)


def _fail_sysprep_task(task_id, message, vmid=None, hostname=None, error_code=None, data=None):
    """Mark task FAILURE and, when a clone exists, rename/tag it for analysis."""
    task = Task.query.get(task_id)
    if not task:
        return
    if task.status == 'CANCELLED':
        _abandon_cancelled_clone(task_id, vmid=vmid, hostname=hostname, data=data)
        return
    task.status = 'FAILURE'
    full = str(message or '')
    task.error_details = full
    # Short badge / list text (DB column historically VARCHAR(512)).
    task.message = full if len(full) <= 512 else (full[:509] + '...')
    append_task_log(task, full)
    if error_code:
        task.error_code = error_code
    if vmid is not None:
        task.result_vmid = vmid
    batch_id = task.batch_id
    db.session.commit()
    if vmid:
        _mark_clone_failed(vmid, hostname=hostname or task.hostname, data=data, stop=False)
    _finalize_batch_if_done(batch_id)


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


@celery.task(bind=True, soft_time_limit=14400, time_limit=14700)
def sysprep_workflow_task(self, task_id, data):
    with app.app_context():
        new_vmid = None
        data = dict(data or {})
        hostname = data.get('hostname')
        try:
            load_task_secrets(task_id, data)
            try:
                pve_override = attach_pve_override(data)
            except ValueError as e:
                _fail_sysprep_task(task_id, str(e), hostname=hostname, error_code='remote', data=data)
                return

            with use_pve_override(pve_override):
                try:
                    require_windows_guest(data['template_vmid'])
                    _validate_sysprep_network(data)
                    _prepare_domain_join(data)
                    from app.domain_preflight import check_domain_join_preflight
                    # Host DC reachability is advisory; in-clone probe is the hard gate.
                    check_domain_join_preflight(data)
                    record_host_dc_preflight(task_id, data)
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
                        task_id, f"Invalid sysprep input: {e}", hostname=hostname, error_code='validation', data=data
                    )
                    return
                except ValueError as e:
                    _fail_sysprep_task(task_id, str(e), hostname=hostname, error_code='validation', data=data)
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
                    _abandon_cancelled_clone(task_id, vmid=new_vmid, hostname=hostname, data=data)
                    return
                set_lifecycle_tag(new_vmid, 'lifecycle-customizing')
                update_task_progress(task_id, 35, "Generating sysprep files...")
                _attach_macs_to_nics(data, new_vmid)
                # Re-pack nics_b64 now that MACs are known.
                _validate_sysprep_network(data)
                unattended_xml, setup_ps1, setup_complete = _render_sysprep_files(data)

                if _task_cancelled(task_id):
                    _abandon_cancelled_clone(task_id, vmid=new_vmid, hostname=hostname, data=data)
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
                    _abandon_cancelled_clone(task_id, vmid=new_vmid, hostname=hostname, data=data)
                    return
                update_task_progress(task_id, 60, "Waiting for QEMU Guest Agent to stabilize...")
                wait_for_guest_agent(
                    new_vmid,
                    timeout=1200,
                    stable_for=agent_stable,
                    on_progress=lambda msg: update_task_progress(task_id, 60, msg),
                )
                update_task_progress(task_id, 70, "QEMU Guest Agent is ready.")

                if _task_cancelled(task_id):
                    _abandon_cancelled_clone(task_id, vmid=new_vmid, hostname=hostname, data=data)
                    return
                if _as_bool(data.get('join_domain'), False):
                    try:
                        from app.domain_guest_probe import probe_domain_credentials_in_guest
                        probe_domain_credentials_in_guest(
                            new_vmid,
                            data,
                            on_progress=lambda msg: update_task_progress(task_id, 72, msg),
                        )
                    except ValidationError as e:
                        _fail_sysprep_task(
                            task_id,
                            str(e),
                            vmid=new_vmid,
                            hostname=hostname,
                            error_code='domain_cred_probe',
                            data=data,
                        )
                        return
                set_lifecycle_tag(new_vmid, 'lifecycle-customizing')
                update_task_progress(task_id, 80, "Resolving product key and writing sysprep files...")
                # Provision the AD computer account now that the clone exists, so
                # specialize can join offline. Soft-fails to late Add-Computer.
                if _as_bool(data.get('join_domain'), False):
                    from app.domain_odj import provision_odj_blob
                    from app.task_options import options_to_json
                    odj_blob = provision_odj_blob(data, hostname)
                    if odj_blob:
                        data['odj_account_data'] = odj_blob
                        data['domain_join_method'] = 'odj'
                    else:
                        data['domain_join_method'] = 'add-computer'
                    join_task = Task.query.get(task_id)
                    if join_task:
                        join_task.options_json = options_to_json(data)
                        record_domain_join_method(join_task, data.get('domain_join_method'))
                        db.session.commit()
                _ensure_server_product_key(data, new_vmid)
                # Re-render after MAC attach + optional Server GVLK injection.
                unattended_xml, setup_ps1, setup_complete = _render_sysprep_files(data)
                _write_sysprep_files(new_vmid, unattended_xml, setup_ps1, setup_complete)
                update_task_progress(task_id, 85, "Sysprep files written successfully.")

                if _task_cancelled(task_id):
                    _abandon_cancelled_clone(task_id, vmid=new_vmid, hostname=hostname, data=data)
                    return
                set_lifecycle_tag(new_vmid, 'lifecycle-sysprep')
                update_task_progress(task_id, 88, "Running Sysprep...")
                sysprep_command = (
                    r'cmd.exe /c "C:\Windows\System32\Sysprep\sysprep.exe '
                    r'/generalize /oobe /shutdown '
                    r'/unattend:C:\Windows\System32\Sysprep\unattended.xml"'
                )
                logging.info(
                    'VM %s [sysprep]: starting sysprep.exe /generalize /oobe /shutdown',
                    new_vmid,
                )
                run_shutdown_command_in_guest(new_vmid, sysprep_command)
                logging.info(
                    'VM %s [sysprep]: sysprep command accepted by guest agent',
                    new_vmid,
                )

                try:
                    power_ok = _complete_sysprep_power_cycle(
                        task_id, new_vmid, progress_base=92, agent_stable_for=agent_stable
                    )
                except SysprepGuestFailed as e:
                    _fail_sysprep_task(
                        task_id,
                        str(e),
                        vmid=new_vmid,
                        hostname=hostname,
                        error_code='sysprep_guest_failed',
                        data=data,
                    )
                    return
                if not power_ok:
                    _fail_sysprep_task(
                        task_id,
                        "Timed out waiting for the VM to shut down (or reboot) after Sysprep.",
                        vmid=new_vmid,
                        hostname=hostname,
                        error_code='sysprep_timeout',
                        data=data,
                    )
                    return

                if _task_cancelled(task_id):
                    _abandon_cancelled_clone(task_id, vmid=new_vmid, hostname=hostname, data=data)
                    return
                set_lifecycle_tag(new_vmid, 'lifecycle-verifying')
                update_task_progress(task_id, 95, "Clone and Sysprep phase complete; queued for verification...")
                # Do not pass admin/domain secrets (or packed join blob) into verify queue.
                scrub_workflow_secrets(data)
                sysprep_verify_task.apply_async(
                    args=(task_id, new_vmid, data),
                    queue='verify_queue',
                )
                return

        except SoftTimeLimitExceeded:
            app.logger.error("Task %s hit soft time limit", task_id)
            _fail_sysprep_task(
                task_id,
                'Workflow exceeded time limit (soft). Check worker logs / guest state.',
                vmid=new_vmid,
                hostname=hostname,
                error_code='time_limit',
                data=data,
            )
        except Exception as e:
            app.logger.error(f"Task {task_id} failed: {e}", exc_info=True)
            _fail_sysprep_task(
                task_id,
                f"An error occurred: {e}",
                vmid=new_vmid,
                hostname=hostname,
                error_code='exception',
                data=data,
            )


@celery.task(bind=True, soft_time_limit=14400, time_limit=14700)
def sysprep_verify_task(self, task_id, vmid, data):
    with app.app_context():
        data = dict(data or {})
        hostname = data.get('hostname')
        if _task_cancelled(task_id):
            _abandon_cancelled_clone(task_id, vmid=vmid, hostname=hostname, data=data)
            return
        try:
            try:
                pve_override = attach_pve_override(data)
            except ValueError as e:
                _fail_sysprep_task(
                    task_id, str(e), vmid=vmid, hostname=hostname, error_code='remote', data=data
                )
                return

            with use_pve_override(pve_override):
                set_lifecycle_tag(vmid, 'lifecycle-verifying')
                update_task_progress(task_id, 98, "Verifying hostname and network via guest agent...")
                expected_ip = None if data.get('use_dhcp') else data.get('ip_address')
                expected_ipv6 = data.get('ipv6_address') if data.get('enable_ipv6') else None
                expect_setup_reboot = bool(data.get('join_domain'))
                if data.get('manage_disks') and data.get('disk_guest_plan'):
                    expect_setup_reboot = expect_setup_reboot or any(
                        d.get('ensure_pagefile') for d in (data.get('disk_guest_plan') or [])
                    )
                verify_summary, verify_ok = _verify_sysprep_result(
                    vmid,
                    data.get('hostname'),
                    expected_ip=expected_ip,
                    expected_domain=data.get('domain_name') if data.get('join_domain') else None,
                    expected_ipv6=expected_ipv6,
                    on_progress=lambda msg: update_task_progress(task_id, 98, msg),
                    expect_setup_reboot=expect_setup_reboot,
                    join_meta={
                        'host_dc_reachable': data.get('host_dc_reachable'),
                        'domain_join_method': data.get('domain_join_method'),
                        'domain_ou': data.get('domain_ou'),
                    },
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
                    _abandon_cancelled_clone(task_id, vmid=vmid, hostname=hostname, data=data)
                    return
                if verify_ok:
                    set_lifecycle_tag(vmid, 'lifecycle-ready')
                    task.status = 'SUCCESS'
                    task.progress = 100
                    task.message = (
                        f"Sysprep workflow for {data['hostname']} completed. "
                        f"Verify: {verify_summary}"
                    )
                    append_task_log(task, task.message)
                    batch_id = task.batch_id
                    db.session.commit()
                    _finalize_batch_if_done(batch_id)
                else:
                    _fail_sysprep_task(
                        task_id,
                        f"Sysprep finished but verification failed for {data['hostname']}: {verify_summary}",
                        vmid=vmid,
                        hostname=hostname,
                        error_code='verify',
                        data=data,
                    )
        except SoftTimeLimitExceeded:
            app.logger.error("Verify task %s hit soft time limit", task_id)
            _fail_sysprep_task(
                task_id,
                'Verify phase exceeded time limit (soft).',
                vmid=vmid,
                hostname=hostname,
                error_code='time_limit',
                data=data,
            )
        except Exception as e:
            app.logger.error(f"Verify task {task_id} failed: {e}", exc_info=True)
            _fail_sysprep_task(
                task_id,
                f"An error occurred during verify phase: {e}",
                vmid=vmid,
                hostname=hostname,
                error_code='verify_exception',
                data=data,
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
            append_task_log(task, task.message)
            db.session.commit()


@celery.task(bind=True, soft_time_limit=14400, time_limit=14700)
def linux_cloudinit_workflow_task(self, task_id, data):
    """Clone a Linux template, apply Proxmox cloud-init, verify via QGA."""
    with app.app_context():
        new_vmid = None
        data = dict(data or {})
        data['os_family'] = 'linux'
        hostname = data.get('hostname')
        try:
            load_task_secrets(task_id, data)
            try:
                pve_override = attach_pve_override(data)
            except ValueError as e:
                _fail_sysprep_task(task_id, str(e), hostname=hostname, error_code='remote', data=data)
                return

            with use_pve_override(pve_override):
                try:
                    require_linux_guest(data['template_vmid'])
                    prepare_linux_payload(data)
                    from app.provision_limits import validate_resource_caps, check_storage_for_template
                    validate_resource_caps(data, 'linux')
                    check_storage_for_template(data['template_vmid'])
                except ValidationError as e:
                    _fail_sysprep_task(
                        task_id,
                        f"Invalid Linux customize input: {e}",
                        hostname=hostname,
                        error_code='validation',
                        data=data,
                    )
                    return
                except ValueError as e:
                    _fail_sysprep_task(
                        task_id, str(e), hostname=hostname, error_code='validation', data=data
                    )
                    return

                if _task_cancelled(task_id):
                    return
                update_task_progress(task_id, 10, "Cloning Linux VM...")
                clone_result = clone_vm(
                    data['template_vmid'],
                    data['hostname'],
                    data['cores'],
                    data['ram'],
                    data['bridge'],
                    data.get('vlan'),
                    nics=clone_nic_list(data),
                    os_family='linux',
                )
                new_vmid = clone_result['vmid']
                update_task_progress(
                    task_id,
                    30,
                    f"VM cloned successfully. New VMID: {new_vmid}",
                    result_vmid=new_vmid,
                )

                if data.get('manage_disks') and data.get('disks'):
                    set_lifecycle_tag(new_vmid, 'lifecycle-customizing')
                    update_task_progress(task_id, 40, "Reconciling Linux disks on Proxmox...")
                    guest_plan = reconcile_linux_vm_disks(new_vmid, data['disks'])
                    data['disk_guest_plan'] = guest_plan
                else:
                    data['disk_guest_plan'] = []

                if _task_cancelled(task_id):
                    _abandon_cancelled_clone(task_id, vmid=new_vmid, hostname=hostname, data=data)
                    return

                set_lifecycle_tag(new_vmid, 'lifecycle-customizing')
                update_task_progress(task_id, 50, "Applying cloud-init network and user...")
                apply_cloudinit_config(
                    new_vmid,
                    nics=data.get('nics'),
                    nameservers=data.get('dns_list') or data.get('dns_servers'),
                    searchdomain=data.get('searchdomain'),
                    ciuser=data.get('ciuser'),
                    sshkeys=data.get('sshkeys'),
                    cipassword=data.get('cipassword'),
                )

                if _task_cancelled(task_id):
                    _abandon_cancelled_clone(task_id, vmid=new_vmid, hostname=hostname, data=data)
                    return

                set_lifecycle_tag(new_vmid, 'lifecycle-booting')
                update_task_progress(task_id, 60, "Powering on VM...")
                power_on_vm(new_vmid)
                boot_settle, agent_stable = _linux_wait_timings(data)
                if boot_settle:
                    update_task_progress(
                        task_id,
                        65,
                        f"Waiting {boot_settle}s for cloud-init boot...",
                    )
                    time.sleep(boot_settle)

                if _task_cancelled(task_id):
                    _abandon_cancelled_clone(task_id, vmid=new_vmid, hostname=hostname, data=data)
                    return

                update_task_progress(task_id, 75, "Waiting for QEMU Guest Agent...")
                wait_for_guest_agent(
                    new_vmid,
                    timeout=900,
                    stable_for=agent_stable,
                    on_progress=lambda msg: update_task_progress(task_id, 75, msg),
                )
                update_task_progress(task_id, 85, "QEMU Guest Agent is ready.")

                if _task_cancelled(task_id):
                    _abandon_cancelled_clone(task_id, vmid=new_vmid, hostname=hostname, data=data)
                    return

                set_lifecycle_tag(new_vmid, 'lifecycle-verifying')
                update_task_progress(task_id, 90, "Queued for Linux verification...")
                scrub_workflow_secrets(data)
                linux_verify_task.apply_async(
                    args=(task_id, new_vmid, data),
                    queue='verify_queue',
                )
                return

        except SoftTimeLimitExceeded:
            app.logger.error("Linux task %s hit soft time limit", task_id)
            _fail_sysprep_task(
                task_id,
                'Linux workflow exceeded time limit (soft).',
                vmid=new_vmid,
                hostname=hostname,
                error_code='time_limit',
                data=data,
            )
        except Exception as e:
            app.logger.error(f"Linux task {task_id} failed: {e}", exc_info=True)
            _fail_sysprep_task(
                task_id,
                f"An error occurred: {e}",
                vmid=new_vmid,
                hostname=hostname,
                error_code='exception',
                data=data,
            )


@celery.task(bind=True, soft_time_limit=7200, time_limit=7500)
def linux_verify_task(self, task_id, vmid, data):
    with app.app_context():
        data = dict(data or {})
        hostname = data.get('hostname')
        if _task_cancelled(task_id):
            _abandon_cancelled_clone(task_id, vmid=vmid, hostname=hostname, data=data)
            return
        try:
            try:
                pve_override = attach_pve_override(data)
            except ValueError as e:
                _fail_sysprep_task(
                    task_id, str(e), vmid=vmid, hostname=hostname, error_code='remote', data=data
                )
                return

            with use_pve_override(pve_override):
                set_lifecycle_tag(vmid, 'lifecycle-verifying')
                update_task_progress(task_id, 95, "Verifying Linux hostname and network...")
                verify_summary, verify_ok = verify_linux_result(
                    vmid,
                    data,
                    timeout=600,
                    on_progress=lambda msg: update_task_progress(task_id, 95, msg),
                )

                task = Task.query.get(task_id)
                if not task:
                    return
                if task.status == 'CANCELLED':
                    _abandon_cancelled_clone(task_id, vmid=vmid, hostname=hostname, data=data)
                    return
                if verify_ok:
                    extra = ''
                    if _as_bool(data.get('detach_cloudinit_after_ready')):
                        update_task_progress(
                            task_id,
                            98,
                            "Powering off, detaching Cloud-Init, powering on...",
                        )
                        try:
                            result = freeze_linux_cloudinit(vmid, clear_ci_fields=True)
                            drives = ','.join(result.get('detached_drives') or []) or 'none'
                            extra = f'; froze cloud-init ({drives})'
                        except Exception as e:  # noqa: BLE001
                            app.logger.warning(
                                "Cloud-init freeze failed for VM %s: %s",
                                vmid, type(e).__name__,
                            )
                            extra = f'; cloud-init freeze failed ({type(e).__name__})'
                    set_lifecycle_tag(vmid, 'lifecycle-ready')
                    task.status = 'SUCCESS'
                    task.progress = 100
                    task.message = (
                        f"Linux cloud-init workflow for {data.get('hostname')} completed. "
                        f"Verify: {verify_summary}{extra}"
                    )
                    append_task_log(task, task.message)
                    batch_id = task.batch_id
                    db.session.commit()
                    _finalize_batch_if_done(batch_id)
                else:
                    _fail_sysprep_task(
                        task_id,
                        f"Linux customize finished but verification failed for "
                        f"{data.get('hostname')}: {verify_summary}",
                        vmid=vmid,
                        hostname=hostname,
                        error_code='verify',
                        data=data,
                    )
        except SoftTimeLimitExceeded:
            app.logger.error("Linux verify task %s hit soft time limit", task_id)
            _fail_sysprep_task(
                task_id,
                'Linux verify exceeded time limit (soft).',
                vmid=vmid,
                hostname=hostname,
                error_code='time_limit',
                data=data,
            )
        except Exception as e:
            app.logger.error(f"Linux verify task {task_id} failed: {e}", exc_info=True)
            _fail_sysprep_task(
                task_id,
                f"An error occurred during Linux verify phase: {e}",
                vmid=vmid,
                hostname=hostname,
                error_code='verify_exception',
                data=data,
            )
