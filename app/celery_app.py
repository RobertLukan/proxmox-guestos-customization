from app import celery, app, db
from app.models import Task
from app.proxmox import (
    clone_vm,
    power_on_vm,
    wait_for_guest_agent,
    write_file_to_guest,
    run_command_in_guest,
    get_vm_ip,
    get_proxmox_api,
    _get_vm_node,
)
from app.validators import (
    ValidationError,
    validate_dns_servers,
    validate_hostname,
    validate_ipv4,
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
    values here (IPs, integer netmask, DNS list) prevents injection into the
    generated PowerShell. Mutates ``data`` in place and raises ValidationError.
    """
    data['ip_address'] = validate_ipv4(data.get('ip_address'), field='IP address')
    data['netmask_cidr'] = validate_netmask(data.get('netmask_cidr'))
    data['gateway'] = validate_ipv4(data.get('gateway'), field='gateway')
    validate_dns_servers(data.get('dns_servers'))  # each entry must be a valid IP
    if data.get('hostname'):
        data['hostname'] = validate_hostname(data['hostname'])


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

            # 2. Generate unattended.xml and setup.ps1
            update_task_progress(task_id, 35, "Generating unattended.xml and setup.ps1...")
            unattended_xml = render_template('sysprep/unattended.xml', **data).encode('utf-8')
            setup_ps1 = render_template('sysprep/setup.ps1', **data).encode('utf-8')

            # 3. Power on the VM
            update_task_progress(task_id, 50, "Powering on VM...")
            power_on_vm(new_vmid)
            update_task_progress(task_id, 55, "Waiting 90 seconds for initial OS reboots...")
            time.sleep(90)

            # 4. Wait for QEMU Guest Agent
            update_task_progress(task_id, 60, "Waiting for QEMU Guest Agent...")
            wait_for_guest_agent(new_vmid, timeout=600)
            update_task_progress(task_id, 70, "QEMU Guest Agent is ready.")

            # 5. Write unattended.xml and setup.ps1 to the guest
            update_task_progress(task_id, 80, "Writing unattended.xml and setup.ps1 to guest...")
            write_file_to_guest(new_vmid, unattended_xml, r'C:\Windows\System32\Sysprep\unattended.xml')
            write_file_to_guest(new_vmid, setup_ps1, r'C:\Windows\Setup\Scripts\setup.ps1')
            update_task_progress(task_id, 85, "unattended.xml and setup.ps1 written successfully.")

            # 6. Run Sysprep
            update_task_progress(task_id, 88, "Running Sysprep...")
            sysprep_command = r'cmd.exe /c "C:\Windows\System32\Sysprep\sysprep.exe /generalize /oobe /shutdown /unattend:C:\Windows\System32\Sysprep\unattended.xml"'
            run_command_in_guest(new_vmid, sysprep_command)

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

            task = Task.query.get(task_id)
            task.status = 'SUCCESS'
            task.progress = 100
            task.message = f"Sysprep workflow for {data['hostname']} completed and verified."
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
            # 0. Validate user-supplied network values before templating.
            try:
                _validate_sysprep_network(data)
            except ValidationError as e:
                task = Task.query.get(task_id)
                task.status = 'FAILURE'
                task.message = f"Invalid sysprep input: {e}"
                db.session.commit()
                return

            # 1. Generate unattended.xml and setup.ps1
            update_task_progress(task_id, 10, "Generating unattended.xml and setup.ps1...")
            unattended_xml = render_template('sysprep/unattended.xml', **data).encode('utf-8')
            setup_ps1 = render_template('sysprep/setup.ps1', **data).encode('utf-8')

            # 2. Wait for QEMU Guest Agent
            update_task_progress(task_id, 25, "Waiting for QEMU Guest Agent...")
            wait_for_guest_agent(vmid, timeout=300)
            update_task_progress(task_id, 40, "QEMU Guest Agent is ready.")

            # 3. Write unattended.xml and setup.ps1 to the guest
            update_task_progress(task_id, 60, "Writing unattended.xml and setup.ps1 to guest...")
            write_file_to_guest(vmid, unattended_xml, r'C:\Windows\System32\Sysprep\unattended.xml')
            write_file_to_guest(vmid, setup_ps1, r'C:\Windows\Setup\Scripts\setup.ps1')
            update_task_progress(task_id, 75, "unattended.xml and setup.ps1 written successfully.")

            # 4. Run Sysprep
            update_task_progress(task_id, 82, "Running Sysprep...")
            sysprep_command = r'cmd.exe /c "C:\Windows\System32\Sysprep\sysprep.exe /generalize /oobe /shutdown /unattend:C:\Windows\System32\Sysprep\unattended.xml"'
            run_command_in_guest(vmid, sysprep_command)

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

            task = Task.query.get(task_id)
            task.status = 'SUCCESS'
            task.progress = 100
            task.message = f"Sysprep for VM {vmid} completed and verified."
            db.session.commit()

        except Exception as e:
            app.logger.error(f"Task {task_id} failed: {e}", exc_info=True)
            task = Task.query.get(task_id)
            task.status = 'FAILURE'
            task.message = f"An error occurred: {e}"
            db.session.commit()