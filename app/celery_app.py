from app import celery, app, db
from app.models import Task
from app.proxmox import (
    clone_vm,
    power_on_vm,
    wait_for_guest_agent,
    write_file_to_guest,
    run_command_in_guest,
    get_vm_ip
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

@celery.task(bind=True)
def sysprep_workflow_task(self, task_id, data):
    with app.app_context():
        try:
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
            update_task_progress(task_id, 90, "Running Sysprep...")
            sysprep_command = r'cmd.exe /c "C:\Windows\System32\Sysprep\sysprep.exe /generalize /oobe /shutdown /unattend:C:\Windows\System32\Sysprep\unattended.xml"'
            run_command_in_guest(new_vmid, sysprep_command)
            update_task_progress(task_id, 95, "Sysprep command issued.")

            # 7. Finalizing
            # The VM will shut down after Sysprep. You might want to add steps to power it back on
            # and verify the configuration. For now, we'll mark the task as complete.
            task = Task.query.get(task_id)
            task.status = 'SUCCESS'
            task.progress = 100
            task.message = f"Sysprep workflow for {data['hostname']} completed successfully."
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
            update_task_progress(task_id, 85, "Running Sysprep...")
            sysprep_command = r'cmd.exe /c "C:\Windows\System32\Sysprep\sysprep.exe /generalize /oobe /shutdown /unattend:C:\Windows\System32\Sysprep\unattended.xml"'
            run_command_in_guest(vmid, sysprep_command)
            update_task_progress(task_id, 95, "Sysprep command issued.")

            # 5. Finalizing
            task = Task.query.get(task_id)
            task.status = 'SUCCESS'
            task.progress = 100
            task.message = f"Sysprep for VM {vmid} completed successfully."
            db.session.commit()

        except Exception as e:
            app.logger.error(f"Task {task_id} failed: {e}", exc_info=True)
            task = Task.query.get(task_id)
            task.status = 'FAILURE'
            task.message = f"An error occurred: {e}"
            db.session.commit()