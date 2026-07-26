import hashlib
import base64
from proxmoxer import ProxmoxAPI
from app import app
import time
import uuid
import re
import logging
import contextvars
from contextlib import contextmanager

# Optional per-task Proxmox connection (set from data['_pve'] in Celery sysprep).
_pve_override = contextvars.ContextVar('pve_override', default=None)


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


def delete_vm(vmid, purge=True, timeout=120):
    """Stop (if needed) and delete a QEMU VM. Used by lab smoke cleanup.

    ``purge`` removes disks from storage (Proxmox delete purge=1).
    """
    proxmox = get_proxmox_api()
    if not proxmox:
        raise Exception("Failed to connect to Proxmox.")
    node = _get_vm_node(vmid)
    if not node:
        raise Exception(f"VM with VMID {vmid} not found.")

    status = proxmox.nodes(node).qemu(vmid).status.current.get().get('status')
    if status == 'running':
        try:
            proxmox.nodes(node).qemu(vmid).status.stop.post()
        except Exception:
            proxmox.nodes(node).qemu(vmid).status.shutdown.post(timeout=60)
        deadline = time.time() + timeout
        while time.time() < deadline:
            cur = proxmox.nodes(node).qemu(vmid).status.current.get().get('status')
            if cur == 'stopped':
                break
            time.sleep(2)
        else:
            raise Exception(f"VM {vmid} did not stop before delete.")

    params = {'purge': 1} if purge else {}
    upid = proxmox.nodes(node).qemu(vmid).delete(**params)
    # Some proxmoxer versions return a UPID string for async delete.
    if isinstance(upid, str) and upid.startswith('UPID:'):
        task_node = upid.split(':')[1]
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = proxmox.nodes(task_node).tasks(upid).status.get()
            if st.get('status') != 'running':
                if st.get('exitstatus') not in (None, 'OK'):
                    raise Exception(f"Delete VM {vmid} failed: {st.get('exitstatus')}")
                return
            time.sleep(2)
        raise Exception(f"Timed out waiting for delete of VM {vmid}.")

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

