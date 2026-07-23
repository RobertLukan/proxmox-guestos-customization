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
    validate_domain,
    validate_hostname,
    validate_ipv4,
    validate_mac,
    validate_netmask,
)
from flask import render_template
import base64
import json
import time


def _as_bool(value):
    """Coerce form/JSON truthy values (True, 'true', 'on', 1) to a bool."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('true', '1', 't', 'yes', 'on')

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
    ValidationError. ``dns_list`` (a list of validated IPs) and ``use_dhcp`` are
    stored back on ``data`` for the templates.

    Two network modes are supported: ``static`` (default; requires IP, netmask
    and gateway) and ``dhcp`` (no static addressing; DNS is optional and, when
    supplied, is applied as an override e.g. to reach the domain controller).
    """
    data['use_dhcp'] = (str(data.get('network_mode') or 'static').lower() == 'dhcp')
    data['dns_list'] = validate_dns_servers(data.get('dns_servers'))
    if not data['use_dhcp']:
        data['ip_address'] = validate_ipv4(data.get('ip_address'), field='IP address')
        data['netmask_cidr'] = validate_netmask(data.get('netmask_cidr'))
        data['gateway'] = validate_ipv4(data.get('gateway'), field='gateway')
    if data.get('hostname'):
        data['hostname'] = validate_hostname(data['hostname'])
    if data.get('primary_mac_address'):
        data['primary_mac_address'] = validate_mac(data['primary_mac_address'])


def _prepare_domain_join(data):
    """Validate domain-join inputs and stage them for the setup.ps1 template.

    When a domain join is requested, credentials are packed into a Base64-encoded
    JSON blob (``domain_join_b64``) so no credential bytes are interpolated into
    PowerShell syntax. The raw password is removed from ``data`` afterwards so it
    does not linger in the task payload/logs. Raises ValidationError on bad input.
    """
    if not _as_bool(data.get('join_domain')):
        data['join_domain'] = False
        return

    domain = validate_domain(data.get('domain_name'))
    username = (data.get('domain_username') or '').strip()
    password = data.get('domain_password')
    if not username or not password:
        raise ValidationError("Domain join requires a username and password.")
    ou = (data.get('domain_ou') or '').strip()

    blob = {'domain': domain, 'username': username, 'password': password}
    if ou:
        blob['ou'] = ou

    data['join_domain'] = True
    data['domain_name'] = domain
    data['domain_ou'] = ou
    data['domain_join_b64'] = base64.b64encode(
        json.dumps(blob).encode('utf-8')
    ).decode('ascii')
    # Do not keep the raw secret around once it is packed into the blob.
    data.pop('domain_password', None)


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


def _verify_sysprep_result(vmid, expected_hostname, expected_ip=None,
                           expected_domain=None, timeout=600):
    """Best-effort post-sysprep verification via the QEMU guest agent (no WinRM).

    Reads back the hostname and an IPv4 address (the specific ``expected_ip`` for
    static configs, or any routable address for DHCP), and — when a domain join
    was requested — the domain membership. Returns a human-readable summary
    string; never raises (verification issues should not fail an otherwise
    successful workflow).
    """
    proxmox = get_proxmox_api()
    node = _get_vm_node(vmid)
    if not proxmox or not node:
        return "verification skipped (VM not found)"

    found_ip = None
    for _ in range(max(1, timeout // 15)):
        try:
            info = proxmox.nodes(node).qemu(vmid).agent.get('network-get-interfaces')
            ips = [
                addr.get('ip-address')
                for iface in info.get('result', [])
                for addr in iface.get('ip-addresses', [])
                if addr.get('ip-address-type') == 'ipv4'
                and not str(addr.get('ip-address', '')).startswith(('127.', '169.254.'))
            ]
            if expected_ip:
                if expected_ip in ips:
                    found_ip = expected_ip
                    break
            elif ips:
                found_ip = ips[0]
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
    parts = [
        f"hostname={actual_hostname or '?'} "
        f"({'ok' if hostname_ok else 'expected ' + str(expected_hostname)})"
    ]

    if expected_ip:
        parts.append(f"IP {expected_ip} {'present' if found_ip else 'NOT yet visible'}")
    else:
        parts.append(f"DHCP IP={found_ip or 'none yet'}")

    if expected_domain:
        domain_info = "unknown"
        try:
            out = run_command_in_guest(
                vmid, 'cmd.exe /c "wmic computersystem get Domain,PartOfDomain /value"')
            if out:
                domain_info = ' '.join(out.split())
        except Exception as e:  # noqa: BLE001
            app.logger.warning(f"Could not read domain membership for VM {vmid}: {e}")
        parts.append(f"domain[{expected_domain}]: {domain_info}")

    return "; ".join(parts)


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
            # 0. Validate user-supplied network + domain values before templating.
            try:
                _validate_sysprep_network(data)
                _prepare_domain_join(data)
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
            verify_summary = _verify_sysprep_result(
                new_vmid,
                data.get('hostname'),
                expected_ip=None if data.get('use_dhcp') else data.get('ip_address'),
                expected_domain=data.get('domain_name') if data.get('join_domain') else None,
            )

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
                _prepare_domain_join(data)
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
            verify_summary = _verify_sysprep_result(
                vmid,
                data.get('hostname'),
                expected_ip=None if data.get('use_dhcp') else data.get('ip_address'),
                expected_domain=data.get('domain_name') if data.get('join_domain') else None,
            )

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