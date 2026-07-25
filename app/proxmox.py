import hashlib
import base64
import json
from proxmoxer import ProxmoxAPI
from app import app, celery, db
from app.models import Task
from app.validators import (
    ValidationError,
    validate_dns_servers,
    validate_hostname,
    validate_ipv4,
    validate_mac,
    validate_netmask,
    validate_vlan,
)
import time
import uuid
import winrm
from flask import url_for
import ipaddress
import re
import logging
import contextvars
from contextlib import contextmanager

# Optional per-task Proxmox connection (set from data['_pve'] in Celery sysprep).
_pve_override = contextvars.ContextVar('pve_override', default=None)


def run_ps_with_params(session, script_body, params):
    """Run a PowerShell script, passing untrusted values via a Base64-encoded
    JSON object decoded into a ``$p`` variable inside the guest.

    User-controlled data is never interpolated into PowerShell syntax: the only
    value placed in the script text is a Base64 string (``[A-Za-z0-9+/=]``),
    which cannot break out of the surrounding single quotes. Scripts should
    reference their inputs as ``$p.fieldName``.
    """
    blob = base64.b64encode(json.dumps(params).encode("utf-8")).decode("ascii")
    prelude = (
        "$p = ConvertFrom-Json ([System.Text.Encoding]::UTF8.GetString("
        f"[System.Convert]::FromBase64String('{blob}')))\n"
    )
    return session.run_ps(prelude + script_body)


@contextmanager
def use_pve_override(override):
    """Temporarily use remote-specific Proxmox credentials (or no-op)."""
    if not override:
        yield
        return
    token = _pve_override.set(override)
    try:
        yield
    finally:
        _pve_override.reset(token)


def get_proxmox_api():
    try:
        override = _pve_override.get()
        if override:
            host = override.get('host')
            user = override.get('user')
            password = override.get('password')
            verify_ssl = override.get('verify_ssl', False)
        else:
            host = app.config['PROXMOX_HOST']
            user = app.config['PROXMOX_USER']
            password = app.config['PROXMOX_PASSWORD']
            verify_ssl = app.config.get('PROXMOX_VERIFY_SSL', False)
        proxmox = ProxmoxAPI(
            host,
            user=user,
            password=password,
            verify_ssl=verify_ssl,
            timeout=300
        )
        return proxmox
    except Exception as e:
        logging.error(f"Error connecting to Proxmox: {e}")
        return None

# Proxmox QEMU ostype values for Windows guests (see qm.conf / PVE docs).
_WINDOWS_OSTYPES = frozenset({
    'wxp', 'w2k', 'w2k3', 'w2k8', 'wvista', 'win7', 'win8', 'win10', 'win11',
})


def is_windows_ostype(ostype):
    """True when ``ostype`` is a Proxmox Windows guest type."""
    if not ostype:
        return False
    return str(ostype).strip().lower() in _WINDOWS_OSTYPES


def get_vm_ostype(vmid, node=None, proxmox=None):
    """Return the QEMU ``ostype`` for ``vmid``, or None on failure."""
    proxmox = proxmox or get_proxmox_api()
    if not proxmox:
        return None
    node = node or _get_vm_node(vmid)
    if not node:
        return None
    try:
        cfg = proxmox.nodes(node).qemu(vmid).config.get()
    except Exception as e:
        logging.warning(f"Could not read ostype for VM {vmid}: {e}")
        return None
    return cfg.get('ostype')


def require_windows_guest(vmid, node=None, proxmox=None):
    """Raise ``ValueError`` unless ``vmid`` has a Windows ``ostype``."""
    ostype = get_vm_ostype(vmid, node=node, proxmox=proxmox)
    if not is_windows_ostype(ostype):
        raise ValueError(
            f"VM {vmid} is not a Windows guest (ostype={ostype!r}). "
            "GuestOS only supports Windows templates and VMs."
        )
    return ostype


def is_proxmox_template(vmid, proxmox=None):
    """True when cluster resources mark ``vmid`` as a QEMU template."""
    proxmox = proxmox or get_proxmox_api()
    if not proxmox:
        return False
    for vm in proxmox.cluster.resources.get(type='vm'):
        if str(vm.get('vmid')) == str(vmid):
            return vm.get('template') == 1
    return False


def require_sysprep_template(template_vmid, proxmox=None):
    """Windows Proxmox template suitable as a golden image for clone+Sysprep.

    In-place Sysprep of ordinary VMs is not allowed (protects production guests).
    """
    proxmox = proxmox or get_proxmox_api()
    if not is_proxmox_template(template_vmid, proxmox=proxmox):
        raise ValueError(
            f"VMID {template_vmid} is not a Proxmox template. "
            "GuestOS customization clones a golden-image template and runs Sysprep "
            "on the clone only — never on an existing/production VM."
        )
    return require_windows_guest(template_vmid, proxmox=proxmox)


def require_sysprep_existing_target(vmid, node=None, proxmox=None):
    """Always reject: in-place Sysprep of existing VMs is disabled."""
    raise ValueError(
        f"In-place Sysprep of VM {vmid} is disabled. "
        "Use a Windows template with Clone + Sysprep (golden image → clone → customize)."
    )


def get_template_vms():
    proxmox = get_proxmox_api()
    if not proxmox:
        return []
    templates = []
    for vm in proxmox.cluster.resources.get(type='vm'):
        if vm.get('template') != 1:
            continue
        node = vm.get('node')
        vmid = vm.get('vmid')
        try:
            cfg = proxmox.nodes(node).qemu(vmid).config.get()
        except Exception as e:
            logging.warning(f"Skipping template {vmid}: cannot read config ({e})")
            continue
        if not is_windows_ostype(cfg.get('ostype')):
            continue
        templates.append(vm)
    return templates

def get_network_bridges():
    proxmox = get_proxmox_api()
    if not proxmox:
        return []
    bridges = []
    nodes = proxmox.nodes.get()
    for node in nodes:
        node_name = node['node']
        networks = proxmox.nodes(node_name).network.get()
        for network in networks:
            if network.get('type') == 'bridge':
                bridges.append(network)
    return bridges

def _get_vm_node(vmid):
    proxmox = get_proxmox_api()
    if not proxmox:
        return None
    for vm in proxmox.cluster.resources.get(type='vm'):
        if str(vm.get('vmid')) == str(vmid):
            return vm.get('node')
    return None

def get_primary_mac_address(vmid, node=None, proxmox=None):
    """Return the MAC address of the VM's primary NIC (net0), or None.

    Used to key guest-side network configuration off a stable identifier rather
    than a fragile adapter name.
    """
    proxmox = proxmox or get_proxmox_api()
    if not proxmox:
        return None
    node = node or _get_vm_node(vmid)
    if not node:
        return None
    try:
        vm_config = proxmox.nodes(node).qemu(vmid).config.get()
    except Exception as e:
        logging.warning(f"Could not read config for VM {vmid}: {e}")
        return None
    net0 = vm_config.get('net0', '')
    match = re.search(r'([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})', net0)
    return match.group(1) if match else None

def select_winrm_ip(vmid, node=None, proxmox=None):
    """Return a VM's IPv4 address suitable for the WinRM connection.

    Picks the first non-loopback, non-APIPA (169.254.x.x) IPv4 address reported
    by the guest agent. When ``WINRM_SUBNET`` is configured, only an address
    within that subnet is accepted; otherwise the first valid address is used.
    Returns ``None`` if no suitable address is found (or on any error).
    """
    proxmox = proxmox or get_proxmox_api()
    if not proxmox:
        return None
    node = node or _get_vm_node(vmid)
    if not node:
        return None

    winrm_network = None
    winrm_subnet_str = app.config.get('WINRM_SUBNET')
    if winrm_subnet_str:
        try:
            winrm_network = ipaddress.ip_network(winrm_subnet_str)
        except ValueError:
            winrm_network = None  # Ignore a malformed subnet and match any IP.

    try:
        network_info = proxmox.nodes(node).qemu(vmid).agent.get('network-get-interfaces')
    except Exception as e:
        logging.warning(f"Could not get network info for VM {vmid}: {e}")
        return None

    for iface in network_info.get('result', []):
        for ip_addr_obj in iface.get('ip-addresses', []):
            if ip_addr_obj.get('ip-address-type') != 'ipv4':
                continue
            ip_candidate = ip_addr_obj.get('ip-address', '')
            if ip_candidate.startswith('127.') or ip_candidate.startswith('169.254.'):
                continue
            if winrm_network:
                if ipaddress.ip_address(ip_candidate) in winrm_network:
                    return ip_candidate
            else:
                return ip_candidate
    return None

def _update_vm_tags(vmid, node, tags_to_add=None, tags_to_remove=None):
    proxmox = get_proxmox_api()
    if not proxmox:
        return False, "Failed to connect to Proxmox."
    try:
        vm_config = proxmox.nodes(node).qemu(vmid).config.get()
        current_tags_str = vm_config.get('tags', '')
        current_tags = set(current_tags_str.split(',')) if current_tags_str else set()
        current_tags.discard('')
        if tags_to_add:
            for current_tag in list(current_tags):
                if current_tag.startswith('lifecycle-'):
                    current_tags.remove(current_tag)
            for tag in tags_to_add:
                current_tags.add(tag)
        if tags_to_remove:
            for tag_prefix in tags_to_remove:
                for current_tag in list(current_tags):
                    if current_tag.startswith(tag_prefix):
                        current_tags.remove(current_tag)
        new_tags_str = ','.join(sorted(list(filter(None, current_tags))))
        proxmox.nodes(node).qemu(vmid).config.set(tags=new_tags_str)
        return True, "VM tags updated successfully."
    except Exception as e:
        return False, f"Failed to update VM tags: {e}"

def get_vm_current_ip(vmid):
    proxmox = get_proxmox_api()
    if not proxmox:
        return None
    node = _get_vm_node(vmid)
    if not node:
        return None
    
    # Poll for a few seconds to handle guest agent instability
    for i in range(5): # 5 attempts over 10 seconds
        try:
            network_info = proxmox.nodes(node).qemu(vmid).agent.get('network-get-interfaces')
            for iface in network_info['result']:
                if 'ip-addresses' in iface:
                    for ip_addr in iface['ip-addresses']:
                        if ip_addr['ip-address-type'] == 'ipv4' and not ip_addr['ip-address'].startswith('127.0.0.1') and not ip_addr['ip-address'].startswith('169.254.'):
                            return ip_addr['ip-address']
        except Exception as e:
            logging.warning(f"Could not get current IP for VM {vmid} on attempt {i+1}: {e}")
        
        time.sleep(2)
        
    return None

def get_manageable_vms():
    """Gets running, non-template Windows VMs with a 'lifecycle-' tag."""
    proxmox = get_proxmox_api()
    if not proxmox:
        return []
    
    manageable_vms = []
    for vm in proxmox.cluster.resources.get(type='vm'):
        # Skip templates and VMs that are not running
        if vm.get('template') == 1 or vm.get('status') != 'running':
            continue

        vm_tags_str = vm.get('tags', '')
        vm_tags = set(vm_tags_str.split(',')) if vm_tags_str else set()
        vm_tags.discard('')

        # Check if any tag starts with 'lifecycle-'
        if not any(tag.startswith('lifecycle-') for tag in vm_tags):
            continue

        node = vm.get('node')
        vmid = vm.get('vmid')
        try:
            cfg = proxmox.nodes(node).qemu(vmid).config.get()
        except Exception as e:
            logging.warning(f"Skipping VM {vmid}: cannot read config ({e})")
            continue
        if not is_windows_ostype(cfg.get('ostype')):
            continue

        manageable_vms.append({
            'vmid': vm.get('vmid'),
            'name': vm.get('name'),
            'status': vm.get('status'),
            'node': vm.get('node'),
            'tags': list(vm_tags),
            'uuid': next((tag.split(':')[1] for tag in vm_tags if tag.startswith('uuid:')), str(uuid.uuid4())), # Assign a new UUID if none exists
        })
    return manageable_vms

def clone_vm(template_vmid, hostname, cores, ram, bridge, vlan):
    """Clones a Windows template VM and returns the new VMID."""
    proxmox = get_proxmox_api()
    if not proxmox:
        raise Exception("Failed to connect to Proxmox.")

    template_info = next((vm for vm in proxmox.cluster.resources.get(type='vm') if str(vm.get('vmid')) == str(template_vmid)), None)
    if not template_info:
        raise Exception(f"Template with VMID {template_vmid} not found.")
    if template_info.get('template') != 1:
        raise Exception(f"VMID {template_vmid} is not a Proxmox template.")

    node = template_info['node']
    require_windows_guest(template_vmid, node=node, proxmox=proxmox)
    new_vmid = proxmox.cluster.nextid.get()
    
    clone_params = {'newid': new_vmid, 'name': hostname, 'full': 1}
    upid = proxmox.nodes(node).qemu(template_vmid).clone.post(**clone_params)
    task_node = upid.split(':')[1]

    while proxmox.nodes(task_node).tasks(upid).status.get()['status'] == 'running':
        time.sleep(2)

    if proxmox.nodes(task_node).tasks(upid).status.get().get('exitstatus') != 'OK':
        raise Exception("Cloning failed.")

    vm_uuid = str(uuid.uuid4())
    _update_vm_tags(new_vmid, node, tags_to_add=[f'uuid:{vm_uuid}', 'lifecycle-cloning'])

    # Preserve the cloned NIC MAC; only retarget bridge/VLAN (and ensure virtio).
    existing_net0 = proxmox.nodes(node).qemu(new_vmid).config.get().get('net0', '') or ''
    mac_match = re.search(r'([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})', existing_net0)
    if mac_match:
        net0_config = f'virtio={mac_match.group(1)},bridge={bridge}'
    else:
        net0_config = f'virtio,bridge={bridge}'
    if vlan:
        net0_config += f',tag={vlan}'
    proxmox.nodes(node).qemu(new_vmid).config.post(cores=cores, memory=ram, net0=net0_config, agent=1)
    
    return {'vmid': new_vmid, 'uuid': vm_uuid}

def power_on_vm(vmid):
    """Powers on a VM."""
    proxmox = get_proxmox_api()
    node = _get_vm_node(vmid)
    if not node:
        raise Exception(f"VM with VMID {vmid} not found.")
    
    vm_status = proxmox.nodes(node).qemu(vmid).status.current.get()
    if vm_status.get('status') != 'running':
        proxmox.nodes(node).qemu(vmid).status.start.post()
        for _ in range(10): # 50-second timeout
            time.sleep(5)
            vm_status = proxmox.nodes(node).qemu(vmid).status.current.get()
            if vm_status.get('status') == 'running':
                return
        raise Exception(f"VM {vmid} failed to power on.")

def wait_for_guest_agent(vmid, timeout=1200, stable_for=45):
    """Wait until the QEMU Guest Agent is responsive *and stays up*.

    Windows 11 (and sometimes Server) can briefly start the guest agent, then
    reboot again during specialize/OOBE. Returning on the first successful poll
    races those reboots and causes later file writes to fail with
    "QEMU guest agent is not running".

    ``stable_for`` is the number of consecutive seconds the agent must keep
    answering before we treat it as ready.
    """
    proxmox = get_proxmox_api()
    node = _get_vm_node(vmid)
    if not node:
        raise Exception(f"VM {vmid} not found.")

    deadline = time.time() + timeout
    ok_since = None
    poll = 5
    while time.time() < deadline:
        try:
            if proxmox.nodes(node).qemu(vmid).agent.get('get-fsinfo') is not None:
                now = time.time()
                if ok_since is None:
                    ok_since = now
                    logging.info(
                        f"Guest agent responded on VM {vmid}; "
                        f"waiting {stable_for}s for stability..."
                    )
                elif now - ok_since >= stable_for:
                    logging.info(f"Guest agent on VM {vmid} stable for {stable_for}s.")
                    return
            else:
                ok_since = None
        except Exception as e:
            if ok_since is not None:
                logging.info(
                    f"Guest agent on VM {vmid} dropped during stability wait "
                    f"(will keep waiting): {e}"
                )
            ok_since = None
        time.sleep(poll)
    raise Exception(
        f"Timed out waiting for a stable QEMU Guest Agent on VM {vmid} "
        f"(timeout={timeout}s, stable_for={stable_for}s)."
    )


def _is_transient_agent_error(exc):
    """True when Proxmox reports the guest agent is temporarily unavailable."""
    msg = str(exc).lower()
    return (
        'guest agent' in msg
        or 'not running' in msg
        or 'timeout' in msg
        or 'connection refused' in msg
    )


def write_file_to_guest(vmid, content, file_path):
    """Writes a file to the guest OS via QEMU Guest Agent."""
    content_b64 = base64.b64encode(content).decode('ascii')

    command = (
        "powershell -command \""
        f"New-Item -ItemType Directory -Force -Path (Split-Path -Path '{file_path}' -Parent); "
        f"[System.IO.File]::WriteAllBytes('{file_path}', "
        f"[System.Convert]::FromBase64String('{content_b64}'))\""
    )
    run_command_in_guest(vmid, command)


def run_command_in_guest(vmid, command, retries=8, retry_delay=15):
    """Run a command in the guest via QEMU Guest Agent, with retries.

    Retries when the agent briefly disappears (common on Windows 11 during
    early boot / specialize). Permanent command failures (non-zero exit) are
    not retried.
    """
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            return _run_command_in_guest_once(vmid, command)
        except Exception as e:
            last_err = e
            # Non-zero exit from the guest command itself is not transient.
            if 'exit code' in str(e).lower() or not _is_transient_agent_error(e):
                raise
            logging.warning(
                f"Guest agent command failed on VM {vmid} "
                f"(attempt {attempt}/{retries}): {e}"
            )
            if attempt == retries:
                break
            try:
                wait_for_guest_agent(vmid, timeout=max(120, retry_delay * 4), stable_for=20)
            except Exception as wait_err:
                logging.warning(f"Re-wait for guest agent on VM {vmid}: {wait_err}")
                time.sleep(retry_delay)
    raise last_err


def _run_command_in_guest_once(vmid, command):
    """Single attempt to run a guest command via the QEMU Guest Agent."""
    proxmox = get_proxmox_api()
    node = _get_vm_node(vmid)
    if not node:
        raise Exception(f"VM {vmid} not found.")

    result = proxmox.nodes(node).qemu(vmid).agent.exec.post(command=command)
    pid = result['pid']

    while True:
        status = proxmox.nodes(node).qemu(vmid).agent('exec-status').get(pid=pid)
        if status.get('exited'):
            if status.get('exitcode') != 0:
                raise Exception(
                    f"Command failed with exit code {status['exitcode']}: "
                    f"{status.get('err-data')}"
                )
            return status.get('out-data')
        time.sleep(2)


def run_shutdown_command_in_guest(vmid, command, settle=30, retries=6):
    """Launch a guest command that is expected to power off/reboot the VM.

    Unlike ``run_command_in_guest``, this does NOT treat the guest agent
    becoming unreachable as an error: a command such as
    ``sysprep ... /shutdown`` intentionally kills the agent when it powers the
    machine down. We briefly poll ``exec-status`` so a command that exits
    non-zero *before* the shutdown still surfaces as an error; once the agent
    disappears (or ``settle`` seconds pass) we return and let the caller confirm
    success by waiting for the VM to actually stop.
    """
    proxmox = get_proxmox_api()
    node = _get_vm_node(vmid)
    if not node:
        raise Exception(f"VM {vmid} not found.")

    last_err = None
    result = None
    for attempt in range(1, retries + 1):
        try:
            result = proxmox.nodes(node).qemu(vmid).agent.exec.post(command=command)
            break
        except Exception as e:
            last_err = e
            if not _is_transient_agent_error(e) or attempt == retries:
                raise
            logging.warning(
                f"Could not issue shutdown command on VM {vmid} "
                f"(attempt {attempt}/{retries}): {e}"
            )
            try:
                wait_for_guest_agent(vmid, timeout=180, stable_for=15)
            except Exception:
                time.sleep(15)
    if result is None:
        raise last_err

    pid = result.get('pid')
    deadline = time.time() + settle
    while time.time() < deadline:
        try:
            status = proxmox.nodes(node).qemu(vmid).agent('exec-status').get(pid=pid)
        except Exception as e:
            # Agent unreachable -> the shutdown has begun. This is expected.
            logging.info(
                f"Guest agent unreachable after issuing shutdown command on "
                f"VM {vmid} (expected): {e}"
            )
            return
        if status.get('exited'):
            exitcode = status.get('exitcode')
            if exitcode not in (0, None):
                raise Exception(
                    f"Command failed with exit code {exitcode}: "
                    f"{status.get('err-data')}"
                )
            return
        time.sleep(2)
    # Command still running after settle window; assume the shutdown is in
    # progress and let the caller verify via VM status.
    return

def get_vm_ip(vmid, timeout=300):
    """Gets the IP address of a VM."""
    proxmox = get_proxmox_api()
    node = _get_vm_node(vmid)
    if not node:
        raise Exception(f"VM with VMID {vmid} not found.")

    logging.info(f"Waiting for IP for VM {vmid} (timeout: {timeout}s)")
    for i in range(timeout // 10):
        try:
            network_info = proxmox.nodes(node).qemu(vmid).agent.get('network-get-interfaces')
            for iface in network_info['result']:
                if 'ip-addresses' in iface:
                    for ip_addr in iface['ip-addresses']:
                        if ip_addr['ip-address-type'] == 'ipv4' and not ip_addr['ip-address'].startswith('127.0.0.1') and not ip_addr['ip-address'].startswith('169.254.'):
                            logging.info(f"Found IP for VM {vmid}: {ip_addr['ip-address']}")
                            return ip_addr['ip-address']
            logging.info(f"No IP found for VM {vmid} on attempt {i+1}")
        except Exception as e:
            logging.warning(f"Could not get network info for VM {vmid} on attempt {i+1}: {e}")
        
        time.sleep(10)
        
    raise Exception(f"Timed out waiting for IP for VM {vmid}.")

@celery.task(bind=True)
def clone_vm_task(self, task_id, template_vmid, hostname, cores, ram, bridge, vlan):
    with app.app_context():
        task = Task.query.get(task_id)
        if not task: return
        task.update_status('STARTED', 0, "Starting VM cloning process...")
        try:
            clone_result = clone_vm(template_vmid, hostname, cores, ram, bridge, vlan)
            new_vmid = clone_result['vmid']
            vm_uuid = clone_result['uuid']
            
            # Stop after clone. Power-on / WinRM wait-for-IP are started only from
            # the workflow UI (or explicit API), so operators can clone now and
            # Sysprep or reconfigure later without an automatic chain.
            task.update_status(
                'SUCCESS',
                100,
                f"Successfully cloned to VM {new_vmid}.",
                result_vmid=new_vmid,
                vm_uuid=vm_uuid,
            )
            _update_vm_tags(new_vmid, _get_vm_node(new_vmid), tags_to_add=['lifecycle-configured'])
        except Exception as e:
            task.update_status('FAILURE', 100, f"An error occurred during cloning: {e}")

@celery.task(bind=True)
def power_on_vm_task(self, task_id, vmid, vm_uuid):
    with app.app_context():
        task = Task.query.get(task_id)
        if not task: return
        task.update_status('STARTED', 0, f"Powering on VM {vmid}...")
        node = _get_vm_node(vmid)
        if not node:
            task.update_status('FAILURE', 100, f"VM with VMID {vmid} not found.")
            return
        _update_vm_tags(vmid, node, tags_to_add=['lifecycle-powering_on'])
        try:
            power_on_vm(vmid)
            task.update_status('PROGRESS', 50, f"VM {vmid} is running. Adding temporary network interface...")
            
            proxmox = get_proxmox_api()
            vm_config = proxmox.nodes(node).qemu(vmid).config.get()
            if 'net1' not in vm_config:
                proxmox.nodes(node).qemu(vmid).config.post(net1=f"virtio,bridge={app.config['TEMP_BRIDGE']}")
                time.sleep(5)

            task.update_status('PROGRESS', 100, f"VM {vmid} is running. Waiting 90 seconds for initial OS reboots...")
            time.sleep(90)
            _update_vm_tags(vmid, node, tags_to_add=['lifecycle-powered_on'])
            wait_for_guest_agent_and_ip_task.delay(task_id, vmid, vm_uuid)
        except Exception as e:
            task.update_status('FAILURE', 100, f"Failed to power on VM {vmid}: {e}")
            _update_vm_tags(vmid, node, tags_to_add=['lifecycle-failed'])

@celery.task(bind=True)
def wait_for_guest_agent_and_ip_task(self, task_id, vmid, vm_uuid):
    with app.app_context():
        task = Task.query.get(task_id)
        if not task: return
        task.update_status('STARTED', 0, f"Waiting for IP for VM {vmid}...", vm_uuid=vm_uuid)
        node = _get_vm_node(vmid)
        if not node:
            task.update_status('FAILURE', 100, f"VM {vmid} not found.")
            return
        _update_vm_tags(vmid, node, tags_to_add=['lifecycle-waiting_for_ip'])
        
        try:
            # Validate WINRM_SUBNET up front so a misconfiguration fails clearly.
            winrm_subnet_str = app.config.get('WINRM_SUBNET')
            if winrm_subnet_str:
                try:
                    ipaddress.ip_network(winrm_subnet_str)
                except ValueError as e:
                    task.update_status('FAILURE', 100, f"Invalid WINRM_SUBNET configured: {e}")
                    return

            selected_ip_address = None
            max_attempts = 30 # Increased attempts for robustness
            for attempt in range(max_attempts):
                task.update_status('PROGRESS', int((attempt / max_attempts) * 100), f"Waiting for WinRM IP... (Attempt {attempt+1}/{max_attempts})")
                selected_ip_address = select_winrm_ip(vmid, node=node)
                if selected_ip_address:
                    break
                time.sleep(10) # Wait before retrying

            if not selected_ip_address:
                task.update_status('FAILURE', 100, f"Timed out waiting for a suitable IP address within {winrm_subnet_str if winrm_subnet_str else 'any network'} for VM {vmid}.")
                _update_vm_tags(vmid, node, tags_to_add=['lifecycle-failed'])
                return

            ip_address = selected_ip_address # Use the selected IP

            primary_mac_address = next((re.search(r'virtio=([0-9A-Fa-f:]{17})', vm_config['net0']).group(1) for vm_config in [get_proxmox_api().nodes(node).qemu(vmid).config.get()] if 'net0' in vm_config and re.search(r'virtio=([0-9A-Fa-f:]{17})', vm_config['net0'])), None)
            if not primary_mac_address:
                task.update_status('FAILURE', 100, "Could not determine primary MAC address.")
                return

            with app.test_request_context():
                redirect_url = url_for('reconfigure_network', vmid=vmid, vm_uuid=vm_uuid, temp_ip_address=ip_address, primary_mac_address=primary_mac_address)
            task.update_status('SUCCESS', 100, f"VM online with temporary IP: {ip_address}.", result_ip_address=ip_address, redirect_url=redirect_url)
            _update_vm_tags(vmid, node, tags_to_add=['lifecycle-ready'])
        except Exception as e:
            task.update_status('FAILURE', 100, f"An unexpected error occurred: {e}")
            _update_vm_tags(vmid, node, tags_to_add=['lifecycle-failed'])

@celery.task(bind=True)
def reconfigure_vm_network_task(self, task_id, vmid, vm_uuid, temp_ip_address, new_ip_address, netmask, gateway, dns_servers, winrm_username, winrm_password, primary_mac_address, remove_temp_interface, join_domain, domain_name, domain_username, domain_password, vlan):
    with app.app_context():
        task = Task.query.get(task_id)
        if not task: return
        task.update_status('STARTED', 5, "Connecting to VM via WinRM...")
        try:
            # Validate temp_ip_address against WINRM_SUBNET
            winrm_subnet_str = app.config.get('WINRM_SUBNET')
            if winrm_subnet_str:
                try:
                    winrm_network = ipaddress.ip_network(winrm_subnet_str)
                    temp_ip = ipaddress.ip_address(temp_ip_address)
                    if temp_ip not in winrm_network:
                        task.update_status('FAILURE', 100, f"Temporary IP address {temp_ip_address} is not within the allowed WinRM subnet {winrm_subnet_str}.")
                        return
                except ValueError as e:
                    task.update_status('FAILURE', 100, f"Invalid WINRM_SUBNET or temporary IP address: {e}")
                    return

            # Validate all user-supplied values before they are sent to the guest.
            try:
                validate_ipv4(temp_ip_address, field="temporary IP address")
                new_ip_address = validate_ipv4(new_ip_address, field="new IP address")
                gateway = validate_ipv4(gateway, field="gateway")
                netmask = validate_netmask(netmask)
                vlan = validate_vlan(vlan)
                dns_list = validate_dns_servers(dns_servers)
                primary_mac_address = validate_mac(primary_mac_address)
                if join_domain:
                    if not domain_name or not domain_username or domain_password is None:
                        raise ValidationError("Domain join requires domain name, username and password.")
            except ValidationError as e:
                task.update_status('FAILURE', 100, f"Invalid reconfiguration input: {e}")
                _update_vm_tags(vmid, _get_vm_node(vmid), tags_to_add=['lifecycle-failed'])
                return

            task.update_status('PROGRESS', 6, "Establishing insecure WinRM connection...")
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            session = winrm.Session(
                f'http://{temp_ip_address}:5985/wsman',
                auth=(winrm_username, winrm_password),
                transport='basic',
                server_cert_validation='ignore'
            )

            proxmox = get_proxmox_api()
            node = _get_vm_node(vmid)
            vm_config = proxmox.nodes(node).qemu(vmid).config.get()
            try:
                hostname = validate_hostname(vm_config.get('name'))
            except ValidationError as e:
                task.update_status('FAILURE', 100, f"VM name is not a valid hostname: {e}")
                _update_vm_tags(vmid, node, tags_to_add=['lifecycle-failed'])
                return

            task.update_status('PROGRESS', 10, "Identifying primary network adapter...")
            r = run_ps_with_params(
                session,
                "(Get-NetAdapter -Physical | Where-Object { $_.MacAddress -eq $p.mac }).Name",
                {"mac": primary_mac_address.replace(':', '-')},
            )
            if r.status_code != 0:
                task.update_status('FAILURE', 100, f"Error getting network adapter: {r.std_err.decode('utf-8')}")
                return
            adapter_name = r.std_out.decode('utf-8').strip()

            task.update_status('PROGRESS', 20, "Clearing existing IP configuration...")
            r = run_ps_with_params(session, 'Get-NetIPAddress -InterfaceAlias $p.adapter | Remove-NetIPAddress -Confirm:$false', {"adapter": adapter_name})
            if r.status_code != 0: logging.warning(f"Non-zero status when clearing IP: {r.std_err.decode('utf-8')}")
            r = run_ps_with_params(session, 'Get-NetRoute -InterfaceAlias $p.adapter | Where-Object { $_.DestinationPrefix -eq "0.0.0.0/0" } | Remove-NetRoute -Confirm:$false', {"adapter": adapter_name})
            if r.status_code != 0: logging.warning(f"Non-zero status when clearing gateway: {r.std_err.decode('utf-8')}")

            task.update_status('PROGRESS', 30, f"Setting new IP: {new_ip_address}...")
            r = run_ps_with_params(
                session,
                'New-NetIPAddress -InterfaceAlias $p.adapter -IPAddress $p.ip -PrefixLength $p.prefix',
                {"adapter": adapter_name, "ip": new_ip_address, "prefix": netmask},
            )
            if r.status_code != 0:
                task.update_status('FAILURE', 100, f"Error setting static IP: {r.std_err.decode('utf-8')}")
                return

            task.update_status('PROGRESS', 40, f"Setting new gateway: {gateway}...")
            r = run_ps_with_params(
                session,
                'New-NetRoute -InterfaceAlias $p.adapter -DestinationPrefix "0.0.0.0/0" -NextHop $p.gateway',
                {"adapter": adapter_name, "gateway": gateway},
            )
            if r.status_code != 0:
                task.update_status('FAILURE', 100, f"Error setting gateway: {r.std_err.decode('utf-8')}")
                return

            if dns_list:
                task.update_status('PROGRESS', 50, f"Setting DNS servers: {', '.join(dns_list)}...")
                r = run_ps_with_params(
                    session,
                    'Set-DnsClientServerAddress -InterfaceAlias $p.adapter -ServerAddresses $p.dns',
                    {"adapter": adapter_name, "dns": dns_list},
                )
                if r.status_code != 0:
                    task.update_status('FAILURE', 100, f"Error setting DNS: {r.std_err.decode('utf-8')}")
                    return
            
            if vlan:
                task.update_status('PROGRESS', 55, f"Setting VLAN tag to {vlan}...")
                net0_config = proxmox.nodes(node).qemu(vmid).config.get().get('net0')
                if net0_config:
                    parts = net0_config.split(',')
                    parts = [p for p in parts if not p.startswith('tag=')]
                    parts.append(f'tag={vlan}')
                    new_net0_config = ','.join(parts)
                    proxmox.nodes(node).qemu(vmid).config.post(net0=new_net0_config)
            
            task.update_status('PROGRESS', 60, f"Renaming computer to '{hostname}' and rebooting...")
            try:
                run_ps_with_params(session, 'Rename-Computer -NewName $p.hostname -Force -Restart', {"hostname": hostname})
            except Exception as e:
                logging.info(f"Ignoring expected error during reboot after rename: {e}")

            task.update_status('PROGRESS', 65, "Waiting for VM to reboot and for new hostname to apply...")
            max_attempts_rename = 30
            rename_verified = False
            for i in range(max_attempts_rename):
                time.sleep(10)
                task.update_status('PROGRESS', 65 + int((i / max_attempts_rename) * 10), f"Waiting for guest agent... (Attempt {i+1}/{max_attempts_rename})")
                try:
                    session = winrm.Session(
                        f'http://{temp_ip_address}:5985/wsman',
                        auth=(winrm_username, winrm_password),
                        transport='basic',
                        server_cert_validation='ignore'
                    )
                    r = session.run_ps("hostname")
                    if r.status_code == 0:
                        current_hostname = r.std_out.decode('utf-8').strip().lower()
                        if current_hostname == hostname.lower():
                            task.update_status('PROGRESS', 75, "Hostname change verified.")
                            rename_verified = True
                            break
                except Exception as e:
                    logging.info(f"Could not connect or verify hostname yet (Attempt {i+1}/{max_attempts_rename}): {e}")
            
            if not rename_verified:
                task.update_status('FAILURE', 100, "Timed out verifying hostname change after reboot.")
                _update_vm_tags(vmid, node, tags_to_add=['lifecycle-failed'])
                return

            if join_domain:
                task.update_status('PROGRESS', 78, f"Joining domain '{domain_name}'...")
                ps_script = '''
                $password = $p.password | ConvertTo-SecureString -AsPlainText -Force
                $credential = New-Object System.Management.Automation.PSCredential($p.username, $password)
                Add-Computer -DomainName $p.domainName -Credential $credential -Force -ErrorAction Stop
                '''
                r = run_ps_with_params(session, ps_script, {
                    "domainName": domain_name,
                    "username": domain_username,
                    "password": domain_password,
                })
                if r.status_code != 0:
                    task.update_status('FAILURE', 100, f"Error joining domain: {r.std_err.decode('utf-8')}")
                    return

            task.update_status('PROGRESS', 85, "Shutting down VM for network reconfiguration...")
            try:
                session.run_ps("Stop-Computer -Force")
            except Exception as e:
                logging.info(f"Ignoring expected error during shutdown: {e}")

            # Wait for VM to stop
            for _ in range(30): # 5 minutes timeout
                vm_status = proxmox.nodes(node).qemu(vmid).status.current.get()
                if vm_status.get('status') == 'stopped':
                    break
                time.sleep(10)
            else:
                task.update_status('FAILURE', 100, "Timed out waiting for VM to stop.")
                _update_vm_tags(vmid, node, tags_to_add=['lifecycle-failed'])
                return

            if remove_temp_interface:
                task.update_status('PROGRESS', 98, "Removing temporary interface...")
                try:
                    proxmox.nodes(node).qemu(vmid).config.post(delete='net1')
                except Exception as e:
                    logging.warning(f"Failed to remove net1 while VM was stopped: {e}")
            
            task.update_status('PROGRESS', 87, "Starting VM...")
            proxmox.nodes(node).qemu(vmid).status.start.post()

            task.update_status('PROGRESS', 90, "Waiting for VM to reboot and guest agent to respond...")
            
            max_attempts = 30
            for i in range(max_attempts):
                time.sleep(10)
                task.update_status('PROGRESS', 90 + int((i / max_attempts) * 5), f"Waiting for guest agent... (Attempt {i+1}/{max_attempts})")
                
                try:
                    proxmox = get_proxmox_api()
                    if not proxmox:
                        continue

                    if not proxmox.nodes(node).qemu(vmid).agent.get('get-fsinfo'):
                        continue

                    task.update_status('PROGRESS', 95, "Guest agent online. Verifying configuration...")

                    current_hostname_raw = proxmox.nodes(node).qemu(vmid).agent.exec.post(command="hostname")
                    pid = current_hostname_raw['pid']
                    time.sleep(2)
                    status = proxmox.nodes(node).qemu(vmid).agent('exec-status').get(pid=pid)
                    current_hostname = status.get('out-data', '').strip().lower()
                    
                    hostname_match = current_hostname == hostname.lower()

                    if not hostname_match:
                        continue

                    net_info = proxmox.nodes(node).qemu(vmid).agent.get('network-get-interfaces')['result']
                    current_ip = next((ip['ip-address'] for iface in net_info for ip in iface.get('ip-addresses', []) if ip.get('ip-address') == new_ip_address), None)
                    
                    ip_match = current_ip == new_ip_address

                    if ip_match:
                        # WinRM is disabled centrally via Group Policy, so no
                        # in-guest deactivation step is needed here. The temporary
                        # interface was already removed during the power-cycle.
                        task.update_status('SUCCESS', 100, f"Reconfiguration successful. IP: {current_ip}, Hostname: {current_hostname}.", result_ip_address=current_ip)
                        _update_vm_tags(vmid, node, tags_to_add=['lifecycle-reconfigured'])
                        return
                
                except Exception as e:
                    logging.warning(f"Error during verification (Attempt {i+1}/{max_attempts}): {e}")
            
            task.update_status('FAILURE', 100, "Timed out verifying reconfiguration after reboot.")
            _update_vm_tags(vmid, node, tags_to_add=['lifecycle-failed'])
        except Exception as e:
            task.update_status('FAILURE', 100, f"Reconfiguration failed: {e}")

@celery.task(bind=True)
def prepare_reconfigure_task(self, task_id, vmid, vm_uuid):
    with app.app_context():
        task = Task.query.get(task_id)
        if not task: return
        task.update_status('STARTED', 0, f"Preparing VM {vmid} for reconfiguration...")
        node = _get_vm_node(vmid)
        if not node:
            task.update_status('FAILURE', 100, f"VM {vmid} not found.")
            return
        proxmox = get_proxmox_api()
        if not proxmox:
            task.update_status('FAILURE', 100, "Failed to connect to Proxmox.")
            return
        try:
            vm_config = proxmox.nodes(node).qemu(vmid).config.get()
            if 'net1' not in vm_config:
                task.update_status('PROGRESS', 10, "Adding temporary network interface (net1)...")
                proxmox.nodes(node).qemu(vmid).config.post(net1=f"virtio,bridge={app.config['TEMP_BRIDGE']}")
                time.sleep(5)
            
            task.update_status('PROGRESS', 20, "Waiting for IP on temporary interface...")
            ip_address = get_vm_ip(vmid)
            primary_mac_address = next((re.search(r'virtio=([0-9A-Fa-f:]{17})', vm_config['net0']).group(1) for vm_config in [proxmox.nodes(node).qemu(vmid).config.get()] if 'net0' in vm_config and re.search(r'virtio=([0-9A-Fa-f:]{17})', vm_config['net0'])), None)
            if not primary_mac_address:
                task.update_status('FAILURE', 100, "Could not determine primary MAC address.")
                return
            
            with app.test_request_context():
                redirect_url = url_for('reconfigure_network', vmid=vmid, vm_uuid=vm_uuid, temp_ip_address=ip_address, primary_mac_address=primary_mac_address)
            task.update_status('SUCCESS', 100, f"VM ready for reconfiguration with IP: {ip_address}.", result_ip_address=ip_address, redirect_url=redirect_url)
            _update_vm_tags(vmid, node, tags_to_add=['lifecycle-ready-for-reconfigure'])
        except Exception as e:
            task.update_status('FAILURE', 100, f"Preparation failed: {e}")

def get_vm_details(vmid):
    """Gets details for a specific VM."""
    proxmox = get_proxmox_api()
    if not proxmox:
        return None
    
    node = _get_vm_node(vmid)
    if not node:
        return None
        
    vm_info = proxmox.nodes(node).qemu(vmid).config.get()
    return {
        'vmid': vmid,
        'name': vm_info.get('name'),
    }
