from app import celery, app, db
from app.models import Task, _utcnow
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
    use_pve_override,
    require_windows_guest,
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

def update_task_progress(task_id, progress, message, result_vmid=None, result_ip_address=None):
    """Helper function to update task progress (and optional result fields)."""
    task = Task.query.get(task_id)
    if task:
        task.progress = progress
        task.message = message
        if task.status in (None, 'PENDING'):
            task.status = 'PROGRESS'
        elif task.status == 'STARTED':
            task.status = 'PROGRESS'
        task.updated_at = _utcnow()
        if result_vmid is not None:
            task.result_vmid = result_vmid
        if result_ip_address is not None:
            task.result_ip_address = result_ip_address
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
    """Write the answer file and post-setup scripts into the guest.

    ``setup.ps1`` is stored under ``C:\\ProgramData\\GuestOS\\`` because Sysprep
    ``/generalize`` often removes ``C:\\Windows\\Setup\\Scripts`` (observed on
    Windows Server 2019). Unattend FirstLogonCommands invokes the ProgramData
    copy; SetupComplete.cmd is still written as a best-effort secondary path.
    """
    write_file_to_guest(vmid, unattended_xml, r'C:\Windows\System32\Sysprep\unattended.xml')
    write_file_to_guest(vmid, setup_ps1, r'C:\ProgramData\GuestOS\setup.ps1')
    write_file_to_guest(vmid, setup_complete, r'C:\Windows\Setup\Scripts\SetupComplete.cmd')


def _parse_domain_membership(raw):
    """Parse Domain / PartOfDomain from guest command output.

    Accepts WMIC ``/value`` lines, PowerShell ``ConvertTo-Json``, or a simple
    ``Domain\\tPartOfDomain`` line.
    """
    if not raw:
        return None, None
    text = raw.strip()
    domain = None
    part = None

    # JSON from ConvertTo-Json (single object).
    if text.startswith('{'):
        try:
            obj = json.loads(text)
            domain = obj.get('Domain') or obj.get('domain')
            part = obj.get('PartOfDomain')
            if part is None:
                part = obj.get('partOfDomain')
        except Exception:  # noqa: BLE001
            pass

    if domain is None or part is None:
        for line in text.replace('\r', '\n').split('\n'):
            line = line.strip()
            if not line or '=' not in line:
                continue
            key, _, val = line.partition('=')
            key = key.strip().lower()
            val = val.strip()
            if key == 'domain':
                domain = val
            elif key == 'partofdomain':
                part = val

    if part is not None and not isinstance(part, bool):
        part_s = str(part).strip().lower()
        if part_s in ('true', '1', 'yes'):
            part = True
        elif part_s in ('false', '0', 'no'):
            part = False
        else:
            part = None

    return domain, part


def _domains_match(actual, expected):
    """True if guest Domain matches expected FQDN/NetBIOS (case-insensitive)."""
    if not actual or not expected:
        return False
    a = str(actual).strip().lower().rstrip('.')
    e = str(expected).strip().lower().rstrip('.')
    if a == e:
        return True
    # Accept NetBIOS vs FQDN (LAB vs lab.test).
    if a.split('.')[0] == e.split('.')[0]:
        return True
    return False


def _read_domain_membership(vmid):
    """Query domain membership via guest agent (PowerShell CIM; no WMIC).

    WMIC is removed on modern Windows 11 images, so CIM/JSON is the primary path.
    Returns ``(domain_name_or_None, part_of_domain_or_None)``.
    """
    # Prefer JSON for reliable parsing.
    ps = (
        'powershell.exe -NoProfile -NonInteractive -Command '
        '"Get-CimInstance Win32_ComputerSystem | '
        'Select-Object Domain,PartOfDomain | ConvertTo-Json -Compress"'
    )
    try:
        out = run_command_in_guest(vmid, ps)
        domain, part = _parse_domain_membership(out)
        if domain is not None or part is not None:
            return domain, part
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"PowerShell domain query failed on VM {vmid}: {e}")

    # Fallback: legacy WMIC (Server 2019 / older images).
    try:
        out = run_command_in_guest(
            vmid, 'cmd.exe /c wmic computersystem get Domain,PartOfDomain /value')
        return _parse_domain_membership(out)
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"WMIC domain query failed on VM {vmid}: {e}")
        return None, None


def _verify_sysprep_result(vmid, expected_hostname, expected_ip=None,
                           expected_domain=None, timeout=None, on_progress=None):
    """Best-effort post-sysprep verification via the QEMU guest agent (no WinRM).

    Returns ``(summary, ok)``. ``ok`` is False when a required static IP never
    appears, the hostname does not match, or an expected domain join is not
    observed — callers should treat that as failure for static customization.
    """
    # DHCP: brief check only — no lease must not look like a hang.
    # Static: give the guest more time to apply the address from setup.ps1
    # (FirstLogonCommands runs after AutoLogon).
    # Domain join triggers an extra reboot after Add-Computer — allow more time.
    if timeout is None:
        if expected_domain:
            timeout = 1200
        elif expected_ip:
            timeout = 900
        else:
            timeout = 45
    poll = 15 if (expected_ip or expected_domain) else 5

    proxmox = get_proxmox_api()
    node = _get_vm_node(vmid)
    if not proxmox or not node:
        return "verification skipped (VM not found)", False

    def _progress(msg):
        if on_progress:
            on_progress(msg)

    # Hostname is set in the specialize pass and is available even before a
    # DHCP lease shows up — read it first so the summary is useful when IP lags.
    actual_hostname = None
    try:
        _progress("Verifying hostname via guest agent...")
        out = run_command_in_guest(vmid, 'cmd.exe /c hostname')
        if out:
            actual_hostname = out.strip()
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"Could not read hostname for VM {vmid}: {e}")

    found_ip = None
    polls = max(1, timeout // poll)
    domain_name = None
    part_of_domain = None
    for i in range(polls):
        try:
            mode = f"static {expected_ip}" if expected_ip else "DHCP"
            _progress(f"Checking guest network ({mode}) ({i + 1}/{polls})...")
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
            elif ips:
                found_ip = ips[0]

            domain_ready = True
            if expected_domain:
                _progress(
                    f"Checking domain membership ({expected_domain}) "
                    f"({i + 1}/{polls})..."
                )
                domain_name, part_of_domain = _read_domain_membership(vmid)
                domain_ready = bool(
                    part_of_domain and _domains_match(domain_name, expected_domain)
                )

            ip_ready = (found_ip is not None) if expected_ip else True
            if ip_ready and domain_ready:
                # DHCP (no static expect): exit once we have a lease, or finish
                # polls if domain-only and membership already confirmed.
                if expected_ip or expected_domain:
                    break
                if found_ip:
                    break
        except Exception as e:  # noqa: BLE001
            app.logger.info(f"VM {vmid} agent not ready during verify: {e}")
        if i + 1 < polls:
            time.sleep(poll)

    # Refresh hostname after possible domain-join reboot.
    if expected_domain:
        try:
            out = run_command_in_guest(vmid, 'cmd.exe /c hostname')
            if out:
                actual_hostname = out.strip()
        except Exception as e:  # noqa: BLE001
            app.logger.warning(f"Could not re-read hostname for VM {vmid}: {e}")

    hostname_ok = (
        actual_hostname is not None
        and expected_hostname is not None
        and actual_hostname.lower() == str(expected_hostname).lower()
    )
    parts = [
        f"hostname={actual_hostname or '?'} "
        f"({'ok' if hostname_ok else 'expected ' + str(expected_hostname)})"
    ]

    ip_ok = True
    if expected_ip:
        if found_ip:
            parts.append(f"IP {expected_ip} present")
        else:
            parts.append(f"IP not assigned (expected {expected_ip} not visible)")
            ip_ok = False
    else:
        if found_ip:
            parts.append(f"DHCP IP={found_ip}")
        else:
            parts.append("IP not assigned (no DHCP lease detected)")
            # DHCP lease absence is informational — guest may be offline from DHCP.

    domain_ok = True
    if expected_domain:
        if part_of_domain and _domains_match(domain_name, expected_domain):
            parts.append(f"domain[{expected_domain}]: joined ({domain_name})")
            domain_ok = True
        elif part_of_domain is False:
            parts.append(
                f"domain[{expected_domain}]: not joined "
                f"(workgroup/domain={domain_name or '?'})"
            )
            domain_ok = False
        elif domain_name and _domains_match(domain_name, expected_domain):
            # Domain string matches but PartOfDomain missing/odd — treat as ok.
            parts.append(f"domain[{expected_domain}]: joined ({domain_name})")
            domain_ok = True
        else:
            parts.append(
                f"domain[{expected_domain}]: unknown "
                f"(read Domain={domain_name or '?'}, PartOfDomain={part_of_domain})"
            )
            domain_ok = False

    ok = hostname_ok and ip_ok and domain_ok
    return "; ".join(parts), ok


def _guest_agent_responsive(proxmox, node, vmid):
    """True when the QEMU Guest Agent answers a cheap probe."""
    try:
        return proxmox.nodes(node).qemu(vmid).agent.get('get-fsinfo') is not None
    except Exception:
        return False


def _wait_for_vm_stopped(vmid, timeout=900):
    """Poll until the VM reports 'stopped'. Returns True on success, else False."""
    outcome = _wait_for_sysprep_shutdown(vmid, timeout=timeout, allow_reboot=False)
    return outcome == 'stopped'


def _wait_for_sysprep_shutdown(
    vmid,
    timeout=1200,
    poll=5,
    agent_down_for=45,
    allow_reboot=True,
    on_progress=None,
):
    """Wait until the guest has left the pre-sysprep running state.

    Sysprep is invoked with ``/shutdown``, so the happy path is Proxmox
    ``status=stopped``. A stop-only wait commonly hangs when:

    1. The VM powers off only briefly (or reboots into OOBE) between polls, so
       we never observe ``stopped`` even though sysprep finished.
    2. Progress stays at "waiting for shut down" while the guest is already up.

    Returns:
      ``'stopped'`` — observed ``status=stopped`` (caller should power on).
      ``'running'`` — agent was down ≥ ``agent_down_for`` seconds, then came
                      back while the VM is running (caller should *not* power
                      on again). Only when ``allow_reboot`` is True.
      ``None`` — timed out.
    """
    proxmox = get_proxmox_api()
    node = _get_vm_node(vmid)
    if not proxmox or not node:
        raise Exception(f"VM {vmid} not found.")

    deadline = time.time() + timeout
    agent_down_since = None
    sustained_outage = False
    last_msg = None

    def _progress(msg):
        nonlocal last_msg
        if on_progress and msg != last_msg:
            last_msg = msg
            on_progress(msg)

    while time.time() < deadline:
        status = proxmox.nodes(node).qemu(vmid).status.current.get().get('status')
        if status == 'stopped':
            _progress("VM shut down after Sysprep.")
            return 'stopped'

        agent_up = _guest_agent_responsive(proxmox, node, vmid)
        if not agent_up:
            if agent_down_since is None:
                agent_down_since = time.time()
            down_for = time.time() - agent_down_since
            if down_for >= agent_down_for:
                sustained_outage = True
            _progress(
                f"Guest agent down ({int(down_for)}s); "
                "waiting for power-off or post-sysprep boot..."
            )
        elif sustained_outage and allow_reboot and status == 'running':
            _progress(
                "Guest agent returned while VM stayed running "
                "(post-sysprep boot; stop window missed)."
            )
            return 'running'
        else:
            # Agent still answering — sysprep often runs for several minutes
            # after we issue the command (settle window returns early).
            agent_down_since = None
            _progress("Sysprep still running in guest (agent up)...")

        time.sleep(poll)

    return None


def _complete_sysprep_power_cycle(task_id, vmid, progress_base=88):
    """After sysprep is issued: wait for stop/reboot, power on if needed.

    Returns True on success, False if the wait timed out (caller marks FAILURE).
    """
    def _on_progress(msg):
        update_task_progress(task_id, progress_base, msg)

    update_task_progress(
        task_id,
        progress_base,
        "Sysprep issued. Waiting for VM to shut down or reboot into OOBE...",
    )
    outcome = _wait_for_sysprep_shutdown(
        vmid,
        timeout=1200,
        on_progress=_on_progress,
    )
    if outcome is None:
        return False

    if outcome == 'stopped':
        update_task_progress(
            task_id,
            min(progress_base + 7, 96),
            "VM shut down. Powering back on to verify...",
        )
        power_on_vm(vmid)
    else:
        update_task_progress(
            task_id,
            min(progress_base + 7, 96),
            "VM already running after Sysprep; waiting for guest agent...",
        )

    wait_for_guest_agent(vmid, timeout=1800, stable_for=60)
    return True

@celery.task(bind=True)
def sysprep_workflow_task(self, task_id, data):
    with app.app_context():
        with use_pve_override(data.get('_pve')):
            try:
                # 0. Validate user-supplied network + domain values before templating.
                try:
                    require_windows_guest(data['template_vmid'])
                    _validate_sysprep_network(data)
                    _prepare_domain_join(data)
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
                    data.get('vlan') # Use .get() for the optional vlan
                )
                new_vmid = clone_result['vmid']
                update_task_progress(
                    task_id,
                    25,
                    f"VM cloned successfully. New VMID: {new_vmid}",
                    result_vmid=new_vmid,
                )

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
                update_task_progress(task_id, 55, "Waiting 3 minutes for initial OS reboots...")
                time.sleep(180)

                # 4. Wait for a *stable* QEMU Guest Agent (not just the first ping).
                update_task_progress(task_id, 60, "Waiting for QEMU Guest Agent to stabilize...")
                wait_for_guest_agent(new_vmid, timeout=1200, stable_for=60)
                update_task_progress(task_id, 70, "QEMU Guest Agent is ready.")

                # 5. Write the answer file + post-setup scripts to the guest.
                update_task_progress(task_id, 80, "Writing sysprep files to guest...")
                _write_sysprep_files(new_vmid, unattended_xml, setup_ps1, setup_complete)
                update_task_progress(task_id, 85, "Sysprep files written successfully.")

                # 6. Run Sysprep
                update_task_progress(task_id, 88, "Running Sysprep...")
                sysprep_command = r'cmd.exe /c "C:\Windows\System32\Sysprep\sysprep.exe /generalize /oobe /shutdown /unattend:C:\Windows\System32\Sysprep\unattended.xml"'
                run_shutdown_command_in_guest(new_vmid, sysprep_command)

                # 7. Verify: wait for shutdown or post-sysprep boot, then confirm
                # the guest agent before reporting success.
                if not _complete_sysprep_power_cycle(task_id, new_vmid, progress_base=92):
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

                task = Task.query.get(task_id)
                if verify_ok:
                    task.status = 'SUCCESS'
                    task.progress = 100
                    task.message = f"Sysprep workflow for {data['hostname']} completed. Verify: {verify_summary}"
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
