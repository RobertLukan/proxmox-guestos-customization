from app import celery, app, db
from app.models import Task
from app.proxmox import (
    clone_vm,
    power_on_vm,
    wait_for_guest_agent,
    write_file_to_guest,
    run_command_in_guest,
    run_shutdown_command_in_guest,
    get_vm_ip,
    get_proxmox_api,
    get_primary_mac_address,
    _get_vm_node,
)
from app.validators import (
    ValidationError,
    validate_dns_servers,
    validate_hostname,
    validate_ipv4,
    validate_mac,
    validate_netmask,
)
from flask import render_template
import time

def update_task_progress(task_id, progress, message):
    """Helper function to update task progress."""
    task = Task.query.get(task_id)
    if task:
        task.progress = progress
        task.message = message
        db.session.commit()


def _validate_sysprep_network(data):
    """Validate/normalize network values before they are rendered into the
    sysprep templates.

    setup.ps1 is not HTML/XML so Flask does not autoescape it; validating the
    values here (IPs, integer netmask, DNS list, MAC) prevents injection into
    the generated PowerShell. Mutates ``data`` in place and raises
    ValidationError. ``dns_list`` (a list of validated IPs) is stored back on
    ``data`` for the template to iterate.
    """
    data['ip_address'] = validate_ipv4(data.get('ip_address'), field='IP address')
    data['netmask_cidr'] = validate_netmask(data.get('netmask_cidr'))
    data['gateway'] = validate_ipv4(data.get('gateway'), field='gateway')
    data['dns_list'] = validate_dns_servers(data.get('dns_servers'))
    if data.get('hostname'):
        data['hostname'] = validate_hostname(data['hostname'])
    if data.get('primary_mac_address'):
        data['primary_mac_address'] = validate_mac(data['primary_mac_address'])


def _render_sysprep_files(data):
    """Render the three guest files from the (already validated) ``data``.

    Returns a tuple of (unattended_xml_bytes, setup_ps1_bytes,
    setup_complete_cmd_bytes).
    """
    unattended_xml = render_template('sysprep/unattended.xml', **data).encode('utf-8')
    setup_ps1 = render_template('sysprep/setup.ps1', **data).encode('utf-8')
    setup_complete = render_template('sysprep/SetupComplete.cmd', **data).encode('utf-8')
    return unattended_xml, setup_ps1, setup_complete


def _write_sysprep_files(vmid, unattended_xml, setup_ps1, setup_complete):
    """Write the answer file and post-setup scripts into the guest."""
    write_file_to_guest(vmid, unattended_xml, r'C:\Windows\System32\Sysprep\unattended.xml')
    write_file_to_guest(vmid, setup_ps1, r'C:\Windows\Setup\Scripts\setup.ps1')
    write_file_to_guest(vmid, setup_complete, r'C:\Windows\Setup\Scripts\SetupComplete.cmd')


def _verify_sysprep_result(vmid, expected_hostname, expected_ip, timeout=600):
    """Best-effort post-sysprep verification via the QEMU guest agent (no WinRM).

    Polls for ``expected_ip`` to appear on the guest and reads back the hostname.
    Returns a human-readable summary string; never raises (verification issues
    should not fail an otherwise-successful workflow).
    """
    proxmox = get_proxmox_api()
    node = _get_vm_node(vmid)
    if not proxmox or not node:
        return "verification skipped (VM not found)"

    found_ip = False
    for _ in range(max(1, timeout // 15)):
        try:
            info = proxmox.nodes(node).qemu(vmid).agent.get('network-get-interfaces')
            ips = [
                addr.get('ip-address')
                for iface in info.get('result', [])
                for addr in iface.get('ip-addresses', [])
                if addr.get('ip-address-type') == 'ipv4'
            ]
            if expected_ip in ips:
                found_ip = True
                break
        except Exception as e:  # noqa: BLE001
            app.logger.info(f"VM {vmid} agent not ready during verify: {e}")
        time.sleep(15)

    actual_hostname = None
    try:
        out = run_command_in_guest(vmid, 'cmd.exe /c hostname')
        if out:
            actual_hostname = out.strip()
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"Could not read hostname for VM {vmid}: {e}")

    hostname_ok = (
        actual_hostname is not None
        and expected_hostname is not None
        and actual_hostname.lower() == str(expected_hostname).lower()
    )
    return (
        f"hostname={actual_hostname or '?'} "
        f"({'ok' if hostname_ok else 'expected ' + str(expected_hostname)}); "
        f"IP {expected_ip} {'present' if found_ip else 'NOT yet visible'}"
    )


def _wait_for_vm_stopped(vmid, timeout=900):
    """Poll until the VM reports 'stopped'. Returns True on success, else False."""
    proxmox = get_proxmox_api()
    node = _get_vm_node(vmid)
    if not proxmox or not node:
        raise Exception(f"VM {vmid} not found.")
    for _ in range(max(1, timeout // 10)):
        status = proxmox.nodes(node).qemu(vmid).status.current.get()
        if status.get('status') == 'stopped':
            return True
        time.sleep(10)
    return False

@celery.task(bind=True)
def sysprep_workflow_task(self, task_id, data):
    with app.app_context():
        try:
            # 0. Validate user-supplied network values before templating.
            try:
                _validate_sysprep_network(data)
            except ValidationError as e:
                task = Task.query.get(task_id)
                task.status = 'FAILURE'
                task.message = f"Invalid sysprep input: {e}"
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
                data.get('vlan') # Use .get() for the optional vlan
            )
            new_vmid = clone_result['vmid']
            update_task_progress(task_id, 25, f"VM cloned successfully. New VMID: {new_vmid}")

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
            update_task_progress(task_id, 55, "Waiting 90 seconds for initial OS reboots...")
            time.sleep(90)

            # 4. Wait for QEMU Guest Agent
            update_task_progress(task_id, 60, "Waiting for QEMU Guest Agent...")
            wait_for_guest_agent(new_vmid, timeout=600)
            update_task_progress(task_id, 70, "QEMU Guest Agent is ready.")

            # 5. Write the answer file + post-setup scripts to the guest.
            update_task_progress(task_id, 80, "Writing sysprep files to guest...")
            _write_sysprep_files(new_vmid, unattended_xml, setup_ps1, setup_complete)
            update_task_progress(task_id, 85, "Sysprep files written successfully.")

            # 6. Run Sysprep
            update_task_progress(task_id, 88, "Running Sysprep...")
            sysprep_command = r'cmd.exe /c "C:\Windows\System32\Sysprep\sysprep.exe /generalize /oobe /shutdown /unattend:C:\Windows\System32\Sysprep\unattended.xml"'
            run_shutdown_command_in_guest(new_vmid, sysprep_command)

            # 7. Verify: wait for the shutdown, then boot back up and confirm the
            # guest agent responds before reporting success.
            update_task_progress(task_id, 92, "Sysprep issued. Waiting for VM to shut down...")
            if not _wait_for_vm_stopped(new_vmid, timeout=900):
                task = Task.query.get(task_id)
                task.status = 'FAILURE'
                task.message = "Timed out waiting for the VM to shut down after Sysprep."
                db.session.commit()
                return

            update_task_progress(task_id, 96, "VM shut down. Powering back on to verify...")
            power_on_vm(new_vmid)
            wait_for_guest_agent(new_vmid, timeout=900)

            update_task_progress(task_id, 98, "Verifying hostname and network via guest agent...")
            verify_summary = _verify_sysprep_result(new_vmid, data.get('hostname'), data.get('ip_address'))

            task = Task.query.get(task_id)
            task.status = 'SUCCESS'
            task.progress = 100
            task.message = f"Sysprep workflow for {data['hostname']} completed. Verify: {verify_summary}"
            db.session.commit()

        except Exception as e:
            app.logger.error(f"Task {task_id} failed: {e}", exc_info=True)
            task = Task.query.get(task_id)
            task.status = 'FAILURE'
            task.message = f"An error occurred: {e}"
            db.session.commit()

@celery.task(bind=True)
def sysprep_existing_vm_task(self, task_id, data):
    with app.app_context():
        vmid = data.get('vmid')
        try:
            # 0. Resolve the primary NIC MAC (so setup.ps1 can target the adapter
            #    reliably) and validate all user-supplied network values.
            mac = get_primary_mac_address(vmid)
            if mac:
                data['primary_mac_address'] = mac
            try:
                _validate_sysprep_network(data)
            except ValidationError as e:
                task = Task.query.get(task_id)
                task.status = 'FAILURE'
                task.message = f"Invalid sysprep input: {e}"
                db.session.commit()
                return

            # 1. Render the answer file + post-setup scripts.
            update_task_progress(task_id, 10, "Generating sysprep files...")
            unattended_xml, setup_ps1, setup_complete = _render_sysprep_files(data)

            # 2. Wait for QEMU Guest Agent
            update_task_progress(task_id, 25, "Waiting for QEMU Guest Agent...")
            wait_for_guest_agent(vmid, timeout=300)
            update_task_progress(task_id, 40, "QEMU Guest Agent is ready.")

            # 3. Write the answer file + post-setup scripts to the guest.
            update_task_progress(task_id, 60, "Writing sysprep files to guest...")
            _write_sysprep_files(vmid, unattended_xml, setup_ps1, setup_complete)
            update_task_progress(task_id, 75, "Sysprep files written successfully.")

            # 4. Run Sysprep
            update_task_progress(task_id, 82, "Running Sysprep...")
            sysprep_command = r'cmd.exe /c "C:\Windows\System32\Sysprep\sysprep.exe /generalize /oobe /shutdown /unattend:C:\Windows\System32\Sysprep\unattended.xml"'
            run_shutdown_command_in_guest(vmid, sysprep_command)

            # 5. Verify: wait for shutdown, boot back up, confirm the guest agent.
            update_task_progress(task_id, 88, "Sysprep issued. Waiting for VM to shut down...")
            if not _wait_for_vm_stopped(vmid, timeout=900):
                task = Task.query.get(task_id)
                task.status = 'FAILURE'
                task.message = "Timed out waiting for the VM to shut down after Sysprep."
                db.session.commit()
                return

            update_task_progress(task_id, 95, "VM shut down. Powering back on to verify...")
            power_on_vm(vmid)
            wait_for_guest_agent(vmid, timeout=900)

            update_task_progress(task_id, 98, "Verifying hostname and network via guest agent...")
            verify_summary = _verify_sysprep_result(vmid, data.get('hostname'), data.get('ip_address'))

            task = Task.query.get(task_id)
            task.status = 'SUCCESS'
            task.progress = 100
            task.message = f"Sysprep for VM {vmid} completed. Verify: {verify_summary}"
            db.session.commit()

        except Exception as e:
            app.logger.error(f"Task {task_id} failed: {e}", exc_info=True)
            task = Task.query.get(task_id)
            task.status = 'FAILURE'
            task.message = f"An error occurred: {e}"
            db.session.commit()