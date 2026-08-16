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
# Linux guests commonly use l26 (2.6+ kernel) or legacy l24.
_LINUX_OSTYPES = frozenset({'l24', 'l26'})


def _looks_like_windows_server_name(name):
    """Best-effort name matcher for Windows Server templates (2019/2022/2025+)."""
    s = str(name or '').strip().lower().replace('_', ' ').replace('-', ' ')
    if not s:
        return False
    compact = s.replace(' ', '')
    year_tokens = (
        'server2019', 'windows2019', 'win2019', 'ws2019', 'w2k19', 'srv2019', 'srv19',
        'server2022', 'windows2022', 'win2022', 'ws2022', 'w2k22', 'srv2022', 'srv22',
        'server2025', 'windows2025', 'win2025', 'ws2025', 'w2k25', 'srv2025', 'srv25',
    )
    if any(t in compact for t in year_tokens):
        return True
    # Year + server-ish token (avoid matching desktop names that merely contain a year).
    for year in ('2019', '2022', '2025'):
        if year in compact and any(
            t in compact for t in ('server', 'win', 'ws', 'srv', 'w2k')
        ):
            # Exclude clear desktop/Win11 names.
            if 'windows11' in compact or 'win11' in compact:
                continue
            return True
    return False


def _split_proxmox_tag_list(tags):
    """Split a Proxmox tags string on ``;`` or ``,`` (PVE uses both)."""
    raw = str(tags or '')
    return [p.strip() for p in raw.replace(';', ',').split(',') if p.strip()]


def _parse_proxmox_tags(tags):
    """Return a lowercased set of Proxmox VM tags."""
    return {p.lower() for p in _split_proxmox_tag_list(tags)}


def _proxmox_tag_delimiter(raw_tags):
    """Prefer semicolon when the live config already uses it (PVE default)."""
    raw = str(raw_tags or '')
    if ';' in raw:
        return ';'
    return ','


def _tags_indicate_server(tags):
    """True when Proxmox tags mark a Windows Server / disk-capable template."""
    parts = _parse_proxmox_tags(tags)
    markers = {
        'guestos-disk',
        'guestos-disks',
        'guestos-server2019',
        'guestos-server2022',
        'guestos-server2025',
        'server2019',
        'server2022',
        'server2025',
        'win2019',
        'win2022',
        'win2025',
        'ws2019',
        'ws2022',
        'ws2025',
        # Lab / operator convention: windowsserver2019 / 2022 / 2025
        'windowsserver2019',
        'windowsserver2022',
        'windowsserver2025',
        'windowsserver2016',
    }
    if parts & markers:
        return True
    # Prefix match for windowsserver* / guestos-server*
    for tag in parts:
        if tag.startswith('windowsserver') or tag.startswith('guestos-server'):
            return True
    return False


def _tags_indicate_windows11(tags):
    """True when tags explicitly mark a Windows 11 / VDI desktop template."""
    parts = _parse_proxmox_tags(tags)
    markers = {
        'windows11',
        'win11',
        'guestos-win11',
        'guestos-windows11',
        'vdi',
        'guestos-vdi',
    }
    return bool(parts & markers)


def _looks_like_windows11_name(name):
    s = str(name or '').strip().lower().replace('_', ' ').replace('-', ' ')
    if not s:
        return False
    compact = s.replace(' ', '')
    return (
        'windows11' in compact
        or 'win11' in compact
        or 'w11' in compact
    )


def _template_name_tags(vmid, node=None, proxmox=None):
    """Return ``(name, tags, ostype)`` for a VM/template, best-effort."""
    proxmox = proxmox or get_proxmox_api()
    name = None
    tags = None
    ostype = None
    if proxmox:
        for vm in proxmox.cluster.resources.get(type='vm'):
            if str(vm.get('vmid')) == str(vmid):
                name = vm.get('name')
                tags = vm.get('tags')
                node = node or vm.get('node')
                break
        node = node or _get_vm_node(vmid)
        if node:
            try:
                cfg = proxmox.nodes(node).qemu(vmid).config.get() or {}
            except Exception as e:
                # Inline newline strip so CodeQL recognizes log-injection sanitization.
                safe_vmid = str(vmid).replace('\r', '').replace('\n', '')[:32]
                logging.warning(
                    "Could not read template metadata for VM %s (%s)",
                    safe_vmid,
                    type(e).__name__,
                )
                cfg = {}
            name = name or cfg.get('name')
            tags = tags if tags not in (None, '') else cfg.get('tags')
            ostype = cfg.get('ostype')
    return name, tags, ostype


def is_windows_server_template(vmid, node=None, proxmox=None):
    """True when template metadata indicates Windows Server (2019/2022/2025+).

    Proxmox ``ostype`` cannot reliably distinguish Server from Win11 for all
    templates (often both are ``win10``/``win11``), so this helper uses template
    name and tags (``windowsserver2022``, ``guestos-disk``, …).
    """
    name, tags, _ostype = _template_name_tags(vmid, node=node, proxmox=proxmox)
    if _looks_like_windows_server_name(name):
        return True
    return _tags_indicate_server(tags)


def is_windows_server_2019_template(vmid, node=None, proxmox=None):
    """Deprecated alias for :func:`is_windows_server_template`."""
    return is_windows_server_template(vmid, node=node, proxmox=proxmox)


def classify_windows_guest_family(vmid, node=None, proxmox=None):
    """Classify a Windows template as ``server`` or ``win11`` for resource caps.

    Preference order:
    1. Explicit tags (``windowsserver2022``, ``windows11``, …)
    2. Name heuristics
    3. ``ostype == win11`` → win11; otherwise default to win11/desktop caps
       (safe for VDI; Server must be tagged/named).
    """
    name, tags, ostype = _template_name_tags(vmid, node=node, proxmox=proxmox)
    if _tags_indicate_server(tags) or _looks_like_windows_server_name(name):
        return 'server'
    if _tags_indicate_windows11(tags) or _looks_like_windows11_name(name):
        return 'win11'
    if str(ostype or '').strip().lower() == 'win11':
        return 'win11'
    return 'win11'


def get_template_storage_usage(vmid, proxmox=None):
    """Return used% for the template boot-disk storage (clone target).

    Returns a dict with ``storage``, ``node``, ``used``, ``total``, ``avail``,
    ``used_pct`` (0–100 float), or raises ``ValueError`` / ``Exception`` on
    failure to resolve storage.
    """
    proxmox = proxmox or get_proxmox_api()
    if not proxmox:
        raise Exception('Failed to connect to Proxmox.')
    boot = get_boot_disk_spec(vmid)
    storage = boot['storage']
    node = boot['node']
    try:
        status = proxmox.nodes(node).storage(storage).status.get() or {}
    except Exception as e:
        raise Exception(f'Could not read storage status for {storage!r} on {node}: {e}')
    total = float(status.get('total') or 0)
    used = float(status.get('used') or 0)
    avail = float(status.get('avail') or max(0.0, total - used))
    used_pct = (used / total * 100.0) if total > 0 else 0.0
    return {
        'storage': storage,
        'node': node,
        'used': used,
        'total': total,
        'avail': avail,
        'used_pct': used_pct,
    }


def is_windows_ostype(ostype):
    """True when ``ostype`` is a Proxmox Windows guest type."""
    if not ostype:
        return False
    return str(ostype).strip().lower() in _WINDOWS_OSTYPES


def is_linux_ostype(ostype):
    """True when ``ostype`` is a Proxmox Linux guest type."""
    if not ostype:
        return False
    return str(ostype).strip().lower() in _LINUX_OSTYPES


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
        # Inline newline strip so CodeQL recognizes log-injection sanitization.
        safe_vmid = str(vmid).replace('\r', '').replace('\n', '')[:32]
        logging.warning(
            "Could not read ostype for VM %s (%s)",
            safe_vmid,
            type(e).__name__,
        )
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


def require_linux_guest(vmid, node=None, proxmox=None):
    """Raise ``ValueError`` unless ``vmid`` has a Linux ``ostype``."""
    ostype = get_vm_ostype(vmid, node=node, proxmox=proxmox)
    if not is_linux_ostype(ostype):
        raise ValueError(
            f"VM {vmid} is not a Linux guest (ostype={ostype!r}). "
            "Expected Proxmox ostype l26/l24 for cloud-init customize."
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


def require_linux_template(template_vmid, proxmox=None):
    """Linux Proxmox template suitable for clone + cloud-init customize."""
    proxmox = proxmox or get_proxmox_api()
    if not is_proxmox_template(template_vmid, proxmox=proxmox):
        raise ValueError(
            f"VMID {template_vmid} is not a Proxmox template. "
            "Linux customize clones a cloud-init golden image template only."
        )
    return require_linux_guest(template_vmid, proxmox=proxmox)


def require_sysprep_existing_target(vmid, node=None, proxmox=None):
    """Always reject: in-place Sysprep of existing VMs is disabled."""
    raise ValueError(
        f"In-place Sysprep of VM {vmid} is disabled. "
        "Use a Windows template with Clone + Sysprep (golden image → clone → customize)."
    )


def get_template_vms(family='windows'):
    """Return Proxmox templates filtered by OS family (``windows`` or ``linux``)."""
    proxmox = get_proxmox_api()
    if not proxmox:
        return []
    fam = (family or 'windows').strip().lower()
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
        ostype = cfg.get('ostype')
        if fam == 'linux':
            if not is_linux_ostype(ostype):
                continue
        else:
            if not is_windows_ostype(ostype):
                continue
        templates.append(vm)
    return templates


def get_linux_template_vms():
    """Return Linux (l24/l26) Proxmox templates."""
    return get_template_vms(family='linux')

def get_network_bridges():
    """Return Linux bridges / SDN VNets visible on the Proxmox cluster.

    Bridges are collected from every node, then **deduplicated by ``iface``**
    (one entry per name). GuestOS assumes identically named bridges exist on
    every node where clones may land — the UI shows a single set, not a
    per-host list.
    """
    proxmox = get_proxmox_api()
    if not proxmox:
        return []
    by_iface = {}
    nodes = proxmox.nodes.get()
    for node in nodes:
        node_name = node['node']
        networks = proxmox.nodes(node_name).network.get()
        for network in networks:
            if network.get('type') != 'bridge':
                continue
            iface = network.get('iface')
            if not iface or iface in by_iface:
                continue
            by_iface[iface] = network
    return [by_iface[k] for k in sorted(by_iface.keys())]

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
    """Add/remove tags on a VM. ``tags_to_remove`` entries are prefix matches.

    Parses both Proxmox ``;`` and ``,`` delimiters so lifecycle replace works on
    configs that already use semicolon-separated tags.
    """
    proxmox = get_proxmox_api()
    if not proxmox:
        return False, "Failed to connect to Proxmox."
    try:
        vm_config = proxmox.nodes(node).qemu(vmid).config.get()
        current_tags_str = vm_config.get('tags', '')
        delimiter = _proxmox_tag_delimiter(current_tags_str)
        current_tags = set(_split_proxmox_tag_list(current_tags_str))
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
        new_tags_str = delimiter.join(sorted(list(filter(None, current_tags))))
        proxmox.nodes(node).qemu(vmid).config.set(tags=new_tags_str)
        return True, "VM tags updated successfully."
    except Exception as e:
        return False, f"Failed to update VM tags: {e}"


def set_lifecycle_tag(vmid, lifecycle_tag, extra_tags=None):
    """Set a replacing ``lifecycle-*`` stage tag on the VM (best-effort).

    On ``lifecycle-ready``, also clear sticky ``failed-customization`` so a
    later successful re-customize does not leave a permanent failure tag.
    """
    node = _get_vm_node(vmid)
    if not node:
        return False, f"VM {vmid} not found."
    tags = [lifecycle_tag]
    if extra_tags:
        tags.extend(extra_tags)
    remove = ['failed-customization'] if lifecycle_tag == 'lifecycle-ready' else None
    return _update_vm_tags(vmid, node, tags_to_add=tags, tags_to_remove=remove)


def rename_vm(vmid, name):
    """Rename a Proxmox QEMU guest (best-effort)."""
    proxmox = get_proxmox_api()
    if not proxmox:
        return False, "Failed to connect to Proxmox."
    node = _get_vm_node(vmid)
    if not node:
        return False, f"VM {vmid} not found."
    try:
        safe = re.sub(r'[^A-Za-z0-9._-]', '-', str(name or '').strip())[:63].strip('-._')
        if not safe:
            safe = f'failed-{vmid}'
        proxmox.nodes(node).qemu(vmid).config.set(name=safe)
        return True, safe
    except Exception as e:
        return False, f"Failed to rename VM {vmid}: {e}"


def mark_vm_customization_failed(vmid, hostname=None):
    """Rename clone and tag it for failed customization analysis (no delete)."""
    base = (hostname or f'vm{vmid}').strip() or f'vm{vmid}'
    # Keep under Proxmox name length; prefer failed-<host>.
    failed_name = f'failed-{base}'[:63]
    rename_ok, rename_detail = rename_vm(vmid, failed_name)
    tag_ok, tag_detail = set_lifecycle_tag(
        vmid, 'lifecycle-failed', extra_tags=['failed-customization']
    )
    parts = []
    if rename_ok:
        parts.append(f'renamed to {rename_detail}')
    else:
        parts.append(f'rename skipped: {rename_detail}')
    if tag_ok:
        parts.append('tagged failed-customization')
    else:
        parts.append(f'tag skipped: {tag_detail}')
    return rename_ok and tag_ok, '; '.join(parts)


def get_vm_nic_macs(vmid):
    """Return MAC addresses for net0, net1, … in index order."""
    proxmox = get_proxmox_api()
    if not proxmox:
        return []
    node = _get_vm_node(vmid)
    if not node:
        return []
    cfg = proxmox.nodes(node).qemu(vmid).config.get() or {}
    macs = []
    for i in range(0, 16):
        net = cfg.get(f'net{i}')
        if not net:
            continue
        match = re.search(r'([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})', str(net))
        if match:
            macs.append(match.group(1))
        else:
            macs.append(None)
    return macs


def _build_net_config(bridge, vlan, existing_net=''):
    """Build a virtio netN config string, preserving MAC when present."""
    from app.validators import ValidationError, validate_bridge

    try:
        bridge = validate_bridge(bridge) or ''
    except ValidationError as e:
        raise ValueError(str(e)) from e
    if not bridge:
        raise ValueError('Bridge is required for net config.')
    mac_match = re.search(r'([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})', existing_net or '')
    if mac_match:
        net_config = f'virtio={mac_match.group(1)},bridge={bridge}'
    else:
        net_config = f'virtio,bridge={bridge}'
    if vlan:
        net_config += f',tag={vlan}'
    return net_config


def _is_vmid_collision_error(exc):
    """True when Proxmox rejected create/clone because the VMID is already taken."""
    msg = str(exc or '').lower()
    return (
        'config file already exists' in msg
        or 'already exists on node' in msg
        or re.search(r'vm\s+\d+\s+already exists', msg) is not None
    )


def _allocate_next_vmid(proxmox):
    """Ask cluster for next free VMID; prefer temporary reservation when supported."""
    try:
        # Newer PVE: reserve=1 holds the id briefly so parallel nextid callers diverge.
        return proxmox.cluster.nextid.get(reserve=1)
    except TypeError:
        return proxmox.cluster.nextid.get()
    except Exception as e:
        # Older APIs may reject unknown query params — fall back.
        if 'reserve' in str(e).lower() or 'param' in str(e).lower():
            return proxmox.cluster.nextid.get()
        raise


def clone_vm(template_vmid, hostname, cores, ram, bridge, vlan, max_attempts=5, nics=None,
             os_family='windows'):
    """Clones a template VM and returns the new VMID.

    Retries VMID allocation when concurrent clones race on ``/cluster/nextid``
    (classic ``config file already exists`` under bulk provisioning).

    ``nics`` is an optional list of ``{bridge, vlan}`` for multi-NIC (single
    customize). When omitted, a single ``net0`` is configured from bridge/vlan.

    ``os_family`` is ``windows`` (default) or ``linux`` — selects ostype gate.
    """
    proxmox = get_proxmox_api()
    if not proxmox:
        raise Exception("Failed to connect to Proxmox.")

    template_info = next((vm for vm in proxmox.cluster.resources.get(type='vm') if str(vm.get('vmid')) == str(template_vmid)), None)
    if not template_info:
        raise Exception(f"Template with VMID {template_vmid} not found.")
    if template_info.get('template') != 1:
        raise Exception(f"VMID {template_vmid} is not a Proxmox template.")

    node = template_info['node']
    fam = (os_family or 'windows').strip().lower()
    if fam == 'linux':
        require_linux_guest(template_vmid, node=node, proxmox=proxmox)
    else:
        require_windows_guest(template_vmid, node=node, proxmox=proxmox)

    last_error = None
    new_vmid = None
    for attempt in range(1, max(1, int(max_attempts)) + 1):
        new_vmid = _allocate_next_vmid(proxmox)
        clone_params = {'newid': new_vmid, 'name': hostname, 'full': 1}
        try:
            upid = proxmox.nodes(node).qemu(template_vmid).clone.post(**clone_params)
        except Exception as e:
            last_error = e
            if _is_vmid_collision_error(e) and attempt < max_attempts:
                logging.warning(
                    "Clone VMID collision on %s (attempt %s/%s): %s — retrying with a new id.",
                    new_vmid, attempt, max_attempts, e,
                )
                time.sleep(min(2 * attempt, 5))
                continue
            raise

        task_node = upid.split(':')[1]
        while proxmox.nodes(task_node).tasks(upid).status.get()['status'] == 'running':
            time.sleep(2)

        status = proxmox.nodes(task_node).tasks(upid).status.get() or {}
        exitstatus = status.get('exitstatus')
        if exitstatus == 'OK':
            break

        err_detail = exitstatus or status.get('status') or 'unknown'
        last_error = Exception(f"Cloning failed: {err_detail}")
        if _is_vmid_collision_error(err_detail) and attempt < max_attempts:
            logging.warning(
                "Clone task failed with VMID collision on %s (attempt %s/%s): %s — retrying.",
                new_vmid, attempt, max_attempts, err_detail,
            )
            time.sleep(min(2 * attempt, 5))
            continue
        raise last_error
    else:
        raise Exception(
            f"Cloning failed after {max_attempts} VMID attempts"
            + (f": {last_error}" if last_error else "")
        )

    vm_uuid = str(uuid.uuid4())
    _update_vm_tags(new_vmid, node, tags_to_add=[f'uuid:{vm_uuid}', 'lifecycle-cloning'])

    cfg = proxmox.nodes(node).qemu(new_vmid).config.get() or {}
    nic_list = list(nics) if nics else [{'bridge': bridge, 'vlan': vlan}]
    if not nic_list:
        nic_list = [{'bridge': bridge, 'vlan': vlan}]

    # Drop template NICs beyond the requested count (best-effort).
    for i in range(len(nic_list), 16):
        if cfg.get(f'net{i}'):
            try:
                proxmox.nodes(node).qemu(new_vmid).config.set(delete=f'net{i}')
            except Exception as e:
                logging.warning("Could not delete net%s on VM %s: %s", i, new_vmid, e)

    post_kwargs = {'cores': cores, 'memory': ram, 'agent': 1}
    for i, nic in enumerate(nic_list):
        nic_bridge = (nic.get('bridge') or bridge or 'vmbr0').strip()
        nic_vlan = nic.get('vlan')
        existing = cfg.get(f'net{i}', '') or ''
        post_kwargs[f'net{i}'] = _build_net_config(nic_bridge, nic_vlan, existing)

    proxmox.nodes(node).qemu(new_vmid).config.post(**post_kwargs)

    return {'vmid': new_vmid, 'uuid': vm_uuid}


def apply_cloudinit_config(vmid, *, nics=None, nameservers=None, searchdomain=None,
                           ciuser=None, sshkeys=None, cipassword=None):
    """Set Proxmox cloud-init fields on a clone (ipconfigN, DNS, SSH user/keys).

    ``nics`` entries may include network_mode (dhcp|static), ip_address, netmask,
    gateway, enable_ipv6, ipv6_address, ipv6_prefix, ipv6_gateway, ipv6_mode
    (static|dhcp).
    """
    proxmox = get_proxmox_api()
    node = _get_vm_node(vmid)
    if not proxmox or not node:
        raise Exception(f"VM {vmid} not found.")

    kwargs = {}
    nic_list = list(nics or [])
    if not nic_list:
        nic_list = [{'network_mode': 'dhcp'}]

    for i, nic in enumerate(nic_list):
        mode = str(nic.get('network_mode') or 'dhcp').strip().lower()
        parts = []
        if mode == 'static':
            ip = (nic.get('ip_address') or '').strip()
            prefix = nic.get('netmask')
            if not ip:
                raise ValueError(f'NIC{i} static mode requires ip_address.')
            try:
                prefix_i = int(prefix)
            except (TypeError, ValueError):
                raise ValueError(f'NIC{i} static mode requires netmask (prefix length).')
            parts.append(f'ip={ip}/{prefix_i}')
            gw = (nic.get('gateway') or '').strip()
            if gw:
                parts.append(f'gw={gw}')
        else:
            parts.append('ip=dhcp')

        enable_v6 = nic.get('enable_ipv6') in (True, 'true', '1', 1, 'yes', 'on')
        if enable_v6:
            v6_mode = str(nic.get('ipv6_mode') or 'static').strip().lower()
            if v6_mode == 'dhcp':
                parts.append('ip6=dhcp')
            else:
                ip6 = (nic.get('ipv6_address') or '').strip()
                p6 = nic.get('ipv6_prefix') or 64
                if not ip6:
                    raise ValueError(f'NIC{i} IPv6 static mode requires ipv6_address.')
                parts.append(f'ip6={ip6}/{int(p6)}')
                gw6 = (nic.get('ipv6_gateway') or '').strip()
                if gw6:
                    parts.append(f'gw6={gw6}')
        kwargs[f'ipconfig{i}'] = ','.join(parts)

    dns = nameservers
    if isinstance(dns, str):
        dns = [p.strip() for p in dns.split(',') if p.strip()]
    if dns:
        kwargs['nameserver'] = ' '.join(str(x) for x in dns)
    if searchdomain:
        kwargs['searchdomain'] = str(searchdomain).strip()
    if ciuser:
        kwargs['ciuser'] = str(ciuser).strip()
    if sshkeys:
        # Proxmox expects URL-encoded newlines for multi-key blobs.
        from urllib.parse import quote
        raw = sshkeys if isinstance(sshkeys, str) else '\n'.join(sshkeys)
        kwargs['sshkeys'] = quote(raw.strip() + '\n', safe='')
    if cipassword:
        kwargs['cipassword'] = str(cipassword)

    if kwargs:
        proxmox.nodes(node).qemu(vmid).config.set(**kwargs)
    return True


_CLOUDINIT_DISK_RE = re.compile(r'^(ide|scsi|sata)\d+$', re.I)
_CI_CONFIG_KEYS = frozenset({
    'ciuser', 'cipassword', 'sshkeys', 'nameserver', 'searchdomain',
    'cicustom', 'citype',
})


def find_cloudinit_drive_keys(cfg):
    """Return bus keys (e.g. ``ide0``) that look like Proxmox cloud-init CDs."""
    keys = []
    for k, v in (cfg or {}).items():
        if not _CLOUDINIT_DISK_RE.match(str(k)):
            continue
        blob = str(v or '').lower()
        if 'cloudinit' in blob:
            keys.append(k)
    return keys


def detach_cloudinit_drive(vmid, clear_ci_fields=True):
    """Remove the Cloud-Init CDROM so Proxmox stops re-seeding the guest.

    The VM should be **stopped** — Proxmox often ignores hot-unplug of the
    cloud-init CDROM while running. Prefer :func:`freeze_linux_cloudinit`.

    Guest hostname/network/user already on disk stay as-is. Optionally clears
    ``ipconfig*`` / ``ciuser`` / DNS fields from the VM config so the UI no
    longer looks cloud-init-managed.
    """
    proxmox = get_proxmox_api()
    node = _get_vm_node(vmid)
    if not proxmox or not node:
        raise Exception(f"VM {vmid} not found.")
    status = (proxmox.nodes(node).qemu(vmid).status.current.get() or {}).get('status')
    if status == 'running':
        raise Exception(
            f"VM {vmid} is running; stop the guest before detaching cloud-init "
            "(use freeze_linux_cloudinit)."
        )
    cfg = proxmox.nodes(node).qemu(vmid).config.get() or {}
    drive_keys = find_cloudinit_drive_keys(cfg)
    deleted = list(drive_keys)

    for key in drive_keys:
        try:
            proxmox.nodes(node).qemu(vmid).config.set(delete=key)
        except Exception as e:
            logging.warning(
                "Could not delete cloud-init drive %s on VM %s: %s",
                key, vmid, type(e).__name__,
            )

    # Drop cloud-init drive from boot order if present.
    boot = str(cfg.get('boot') or '')
    if boot.startswith('order=') and drive_keys:
        order = boot[len('order='):]
        parts = [p for p in order.split(';') if p and p not in drive_keys]
        new_boot = f"order={';'.join(parts)}" if parts else ''
        if new_boot != boot:
            try:
                if new_boot:
                    proxmox.nodes(node).qemu(vmid).config.set(boot=new_boot)
                else:
                    proxmox.nodes(node).qemu(vmid).config.set(delete='boot')
            except Exception as e:
                logging.warning(
                    "Could not update boot order after cloud-init detach on VM %s: %s",
                    vmid, type(e).__name__,
                )

    if clear_ci_fields:
        # Re-read after drive deletes.
        cfg = proxmox.nodes(node).qemu(vmid).config.get() or {}
        to_delete = []
        for k in cfg:
            if k in _CI_CONFIG_KEYS or str(k).startswith('ipconfig'):
                to_delete.append(k)
            # Detached cloud-init volumes often land as unusedN.
            if str(k).startswith('unused') and 'cloudinit' in str(cfg.get(k) or '').lower():
                to_delete.append(k)
        if to_delete:
            try:
                proxmox.nodes(node).qemu(vmid).config.set(delete=','.join(to_delete))
                deleted.extend(to_delete)
            except Exception as e:
                logging.warning(
                    "Could not clear cloud-init fields on VM %s: %s",
                    vmid, type(e).__name__,
                )

    return {'detached_drives': drive_keys, 'cleared': deleted}


def freeze_linux_cloudinit(vmid, clear_ci_fields=True):
    """Power off → detach Cloud-Init CDROM → power on (settings stay on disk).

    Hot-removing the cloud-init drive while the guest is running is unreliable
    on Proxmox; a stop/start cycle is required for the detach to stick.
    """
    power_off_vm(vmid, timeout=120)
    result = detach_cloudinit_drive(vmid, clear_ci_fields=clear_ci_fields)
    power_on_vm(vmid)
    return result


_DISK_KEY_RE = re.compile(r'^(scsi|virtio|sata|ide)(\d+)$')
_SIZE_RE = re.compile(r'^(\d+(?:\.\d+)?)([KMGT])?$', re.I)

# Options that are not regular key=value disk flags (parsed specially / skipped).
# Keep media/serial in opts so round-trips and CD-ROM detection work; they are
# excluded from COPYABLE_DISK_OPTS when attaching new volumes.
_DISK_OPTS_SKIP = frozenset({
    'size', 'import-from', 'format', 'volume', 'file',
})


def _is_cdrom_or_iso(parsed, raw_value=None):
    """True for CD-ROM / ISO / cloud-init attachments (not usable data disks)."""
    opts = (parsed or {}).get('opts') or {}
    if str(opts.get('media', '')).lower() == 'cdrom':
        return True
    blob = ' '.join([
        str(raw_value or ''),
        str((parsed or {}).get('head') or ''),
        str((parsed or {}).get('volume') or ''),
    ]).lower()
    if 'media=cdrom' in blob:
        return True
    if 'cloudinit' in blob:
        return True
    if '.iso' in blob or '/iso/' in blob or ':iso/' in blob:
        return True
    return False


def _parse_size_to_gb(token):
    """Parse Proxmox size tokens like 32G / 65536M / bare integers (GB)."""
    if token is None:
        return None
    s = str(token).strip()
    if not s:
        return None
    m = _SIZE_RE.match(s)
    if not m:
        # Bare integer from "storage:16" style new-disk sizes.
        try:
            return int(s)
        except ValueError:
            return None
    num = float(m.group(1))
    unit = (m.group(2) or 'G').upper()
    mult = {'K': 1 / (1024 ** 2), 'M': 1 / 1024, 'G': 1, 'T': 1024}[unit]
    return max(1, int(round(num * mult)))


def _parse_disk_value(raw):
    """Split a Proxmox disk config value into storage, volume/size, options, size_gb."""
    parts = [p for p in str(raw or '').split(',') if p != '']
    if not parts:
        return None
    head = parts[0]
    opts = {}
    size_gb = None
    for p in parts[1:]:
        if '=' not in p:
            continue
        k, v = p.split('=', 1)
        k = k.strip()
        v = v.strip()
        if k == 'size':
            size_gb = _parse_size_to_gb(v)
        elif k not in _DISK_OPTS_SKIP:
            opts[k] = v
    storage = None
    volume = head
    if ':' in head:
        storage, rest = head.split(':', 1)
        volume = rest
        # Unused slot style: storage:16 (size in GB)
        if rest.isdigit() and size_gb is None:
            size_gb = int(rest)
    return {
        'head': head,
        'storage': storage,
        'volume': volume,
        'opts': opts,
        'size_gb': size_gb,
        'raw': raw,
    }


def list_vm_disks(vmid):
    """Return bus disk entries for a QEMU VM (scsi/virtio/sata/ide).

    Skips CD-ROM/ISO/cloud-init. Firmware volumes (``efidiskN``, ``tpmstateN``)
    and detached ``unusedN`` slots are not bus keys and are never returned.
    """
    proxmox = get_proxmox_api()
    node = _get_vm_node(vmid)
    if not proxmox or not node:
        raise Exception(f'VM with VMID {vmid} not found.')
    cfg = proxmox.nodes(node).qemu(vmid).config.get() or {}
    disks = []
    for key, val in cfg.items():
        m = _DISK_KEY_RE.match(key)
        if not m:
            continue
        parsed = _parse_disk_value(val)
        if not parsed:
            continue
        # CD-ROM / ISO / cloudinit — never treat as attachable data disks.
        if _is_cdrom_or_iso(parsed, val):
            continue
        disks.append({
            'key': key,
            'bus': m.group(1),
            'index': int(m.group(2)),
            **parsed,
            'serial': parsed['opts'].get('serial'),
        })
    disks.sort(key=lambda d: (d['bus'], d['index']))
    return disks, cfg, node, proxmox


def get_boot_disk_spec(vmid):
    """Identify the boot/OS disk and return storage, bus, copyable options, key."""
    disks, cfg, node, proxmox = list_vm_disks(vmid)
    if not disks:
        raise Exception(f'VM {vmid} has no usable disks.')

    boot_key = None
    boot_order = str(cfg.get('boot') or '')
    # e.g. order=scsi0;ide2;net0
    for token in re.findall(r'(scsi|virtio|sata|ide)\d+', boot_order):
        pass
    m = re.search(r'(scsi|virtio|sata|ide)\d+', boot_order)
    if m:
        # Prefer first disk-like device in boot order
        for part in re.findall(r'((?:scsi|virtio|sata|ide)\d+)', boot_order):
            if any(d['key'] == part for d in disks):
                boot_key = part
                break

    boot = None
    if boot_key:
        boot = next((d for d in disks if d['key'] == boot_key), None)
    if not boot:
        # Prefer scsi0, then virtio0, sata0, else lowest index.
        for pref in ('scsi0', 'virtio0', 'sata0', 'ide0'):
            boot = next((d for d in disks if d['key'] == pref), None)
            if boot:
                break
    if not boot:
        boot = disks[0]

    opts = {k: v for k, v in boot['opts'].items() if k in (
        'aio', 'discard', 'cache', 'iothread', 'ssd', 'backup', 'replicate',
        'detect_zeroes', 'queue-size', 'blocksize',
    )}
    if not boot.get('storage'):
        raise Exception(f'Boot disk {boot["key"]} has no storage id.')
    return {
        'key': boot['key'],
        'bus': boot['bus'],
        'index': boot['index'],
        'storage': boot['storage'],
        'opts': opts,
        'size_gb': boot.get('size_gb'),
        'raw': boot.get('raw'),
        'disks': disks,
        'node': node,
        'proxmox': proxmox,
        'cfg': cfg,
    }


def _next_bus_index(disks, bus):
    used = {d['index'] for d in disks if d['bus'] == bus}
    n = 0
    while n in used:
        n += 1
    return n


def _format_disk_value(storage, size_gb, opts, serial=None):
    parts = [f'{storage}:{int(size_gb)}']
    merged = dict(opts or {})
    if serial:
        merged['serial'] = serial
    for k in sorted(merged.keys()):
        parts.append(f'{k}={merged[k]}')
    return ','.join(parts)


def _build_disk_config_value(raw_value, serial=None, extra_opts=None):
    """Rebuild a disk config string: keep volume head + size, merge opts/serial."""
    parsed = _parse_disk_value(raw_value)
    if not parsed:
        return raw_value
    opts = dict(parsed['opts'])
    if extra_opts:
        opts.update({k: v for k, v in extra_opts.items() if v is not None})
    if serial:
        opts['serial'] = serial
    parts = [parsed['head']]
    size_token = None
    for p in str(raw_value).split(',')[1:]:
        if p.startswith('size='):
            size_token = p
            break
    if size_token:
        parts.append(size_token)
    for k in sorted(opts.keys()):
        if k == 'size':
            continue
        parts.append(f'{k}={opts[k]}')
    return ','.join(parts)


def _set_disk_serial(proxmox, node, vmid, disk_key, raw_value, serial, extra_opts=None):
    """Ensure serial= (and optional copyable opts) on an existing disk config."""
    new_val = _build_disk_config_value(raw_value, serial=serial, extra_opts=extra_opts)
    if new_val == raw_value:
        return raw_value
    proxmox.nodes(node).qemu(vmid).config.post(**{disk_key: new_val})
    return new_val


def _resize_disk_to_gb(proxmox, node, vmid, disk_key, current_gb, target_gb):
    if current_gb is None or target_gb is None:
        return False
    if target_gb <= current_gb:
        return False
    # Absolute size resize (Proxmox accepts e.g. 80G).
    proxmox.nodes(node).qemu(vmid).resize.put(disk=disk_key, size=f'{int(target_gb)}G')
    return True


def _pick_non_boot_disk(non_boot, item):
    """Select an unused non-boot disk for a plan entry.

    Preference: ``source_key`` → matching serial → size best-fit → first unused.
    Mutates ``non_boot`` by removing the chosen disk. Returns the disk or None.
    """
    source_key = (item.get('source_key') or '').strip().lower()
    serial = str(item.get('serial') or '')
    size_gb = int(item.get('size_gb') or 0)

    if source_key:
        for i, candidate in enumerate(non_boot):
            if str(candidate.get('key') or '').lower() == source_key:
                return non_boot.pop(i)

    for i, candidate in enumerate(non_boot):
        if str(candidate.get('serial') or '') == serial:
            return non_boot.pop(i)

    if size_gb > 0:
        exact = [
            (i, c) for i, c in enumerate(non_boot)
            if c.get('size_gb') is not None and int(c['size_gb']) == size_gb
        ]
        if exact:
            # Prefer lowest bus index among exact matches.
            exact.sort(key=lambda t: (t[1].get('bus') or '', t[1].get('index') or 0))
            return non_boot.pop(exact[0][0])

        fitting = [
            (i, c) for i, c in enumerate(non_boot)
            if c.get('size_gb') is not None and int(c['size_gb']) >= size_gb
        ]
        if fitting:
            # Smallest surplus (best fit), then lowest bus index.
            fitting.sort(
                key=lambda t: (
                    int(t[1]['size_gb']) - size_gb,
                    t[1].get('bus') or '',
                    t[1].get('index') or 0,
                )
            )
            return non_boot.pop(fitting[0][0])

    if non_boot:
        return non_boot.pop(0)
    return None


def inventory_vm_disks(vmid):
    """Return a JSON-friendly disk inventory for the template planner UI/API.

    Includes bus disks only (no CD-ROM / EFI / TPM). Marks the boot disk.
    """
    boot = get_boot_disk_spec(vmid)
    disks = []
    for d in boot['disks']:
        disks.append({
            'key': d['key'],
            'bus': d['bus'],
            'index': d['index'],
            'size_gb': d.get('size_gb'),
            'serial': d.get('serial') or None,
            'storage': d.get('storage'),
            'is_boot': d['key'] == boot['key'],
        })
    return {
        'vmid': int(vmid),
        'boot_key': boot['key'],
        'disks': disks,
    }


def reconcile_vm_disks(vmid, disks_plan):
    """Attach/grow disks per plan. Returns guest_plan list for setup.ps1 / verify.

    New disks use the boot disk storage and copied options (aio, discard, …).

    EFI (``efidiskN``) and TPM (``tpmstateN``) volumes are not bus disks and are
    never reused. Bus indices for new attaches are tracked in-memory so a stale
    ``list_vm_disks`` refresh cannot reassign ``scsi1`` and push the previous
    volume to ``unusedN`` (Proxmox replaces the slot).

    Non-boot reuse order: plan ``source_key`` → serial → size best-fit → first
    unused → attach new.
    """
    from app.disks import COPYABLE_DISK_OPTS  # local import avoids cycles

    boot = get_boot_disk_spec(vmid)
    proxmox, node = boot['proxmox'], boot['node']
    disks = list(boot['disks'])
    copy_opts = {k: v for k, v in boot['opts'].items() if k in COPYABLE_DISK_OPTS}
    guest_plan = []
    bus = boot['bus']
    # Track indices locally — do not rely solely on re-listing after each attach.
    used_indices = {d['index'] for d in disks if d['bus'] == bus}

    def _alloc_bus_key():
        n = 0
        while n in used_indices:
            n += 1
        used_indices.add(n)
        return f'{bus}{n}'

    # Map existing non-boot disks for reuse. Never treat firmware volumes as
    # candidates (they are not in ``disks``).
    non_boot = [d for d in disks if d['key'] != boot['key']]

    for item in disks_plan:
        role = item['role']
        serial = item['serial']

        if role == 'os':
            target = item.get('grow_to_gb') or item.get('min_size_gb')
            grown = False
            if target:
                grown = _resize_disk_to_gb(
                    proxmox, node, vmid, boot['key'], boot.get('size_gb'), target
                )
            _set_disk_serial(proxmox, node, vmid, boot['key'], boot['raw'], serial)
            guest_plan.append({
                'role': 'os',
                'serial': serial,
                'drive_letter': 'C',
                'min_size_gb': item.get('min_size_gb') or target or boot.get('size_gb') or 1,
                'ensure_pagefile': False,
                'reformat': False,
                'extend': bool(grown or item.get('grow_to_gb')),
                'label': item.get('label'),
                'pve_key': boot['key'],
            })
            continue

        matched = _pick_non_boot_disk(non_boot, item)

        size_gb = int(item['size_gb'])
        if matched:
            # Reused template disks often lack boot-disk opts (discard/ssd/…).
            # Align them with the boot disk the same way new attaches do.
            # Refuse blind reformat of a volume that already has a *different*
            # serial unless the plan explicitly asks to reformat.
            existing_serial = str(matched.get('serial') or '')
            if (
                existing_serial
                and existing_serial != str(serial)
                and not bool(item.get('reformat'))
            ):
                raise Exception(
                    f"Refusing to overwrite disk {matched['key']} "
                    f"(serial={existing_serial}) with role={role} serial={serial} "
                    f"without reformat=true."
                )
            _set_disk_serial(
                proxmox, node, vmid, matched['key'], matched['raw'], serial,
                extra_opts=copy_opts,
            )
            cur = matched.get('size_gb')
            if cur is not None and size_gb > cur:
                _resize_disk_to_gb(proxmox, node, vmid, matched['key'], cur, size_gb)
            pve_key = matched['key']
            used_indices.add(matched['index'])
        else:
            pve_key = _alloc_bus_key()
            value = _format_disk_value(boot['storage'], size_gb, copy_opts, serial=serial)
            proxmox.nodes(node).qemu(vmid).config.post(**{pve_key: value})

        guest_plan.append({
            'role': role,
            'serial': serial,
            'drive_letter': item['drive_letter'],
            'min_size_gb': size_gb,
            'ensure_pagefile': bool(item.get('ensure_pagefile')),
            'reformat': bool(item.get('reformat')),
            'extend': True,
            'label': item.get('label') or ('Pagefile' if role == 'pagefile' else 'Data'),
            'pve_key': pve_key,
        })

    return guest_plan

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


def power_off_vm(vmid, timeout=60):
    """Hard-stop a VM if running (best-effort for cancelled/failed clones)."""
    proxmox = get_proxmox_api()
    if not proxmox:
        raise Exception("Failed to connect to Proxmox.")
    node = _get_vm_node(vmid)
    if not node:
        raise Exception(f"VM with VMID {vmid} not found.")
    status = proxmox.nodes(node).qemu(vmid).status.current.get().get('status')
    if status != 'running':
        return
    try:
        proxmox.nodes(node).qemu(vmid).status.stop.post()
    except Exception:
        proxmox.nodes(node).qemu(vmid).status.shutdown.post(timeout=timeout)
    deadline = time.time() + timeout
    while time.time() < deadline:
        cur = proxmox.nodes(node).qemu(vmid).status.current.get().get('status')
        if cur == 'stopped':
            return
        time.sleep(2)
    raise Exception(f"VM {vmid} did not stop within {timeout}s.")


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

def wait_for_guest_agent(
    vmid,
    timeout=1200,
    stable_for=45,
    on_progress=None,
    drop_reset=20,
    poll=5,
):
    """Wait until the QEMU Guest Agent is responsive enough to use.

    Windows often flaps the agent during specialize/OOBE (brief timeouts while
    the service stays "running"). Requiring one unbroken ``stable_for`` window
    made post-Sysprep waits look stuck: every blip restarted a 60s timer.

    Instead we **accumulate** successful poll time and only reset that progress
    after the agent stays down for ``drop_reset`` seconds (a real reboot / long
    outage). Short blips pause accumulation but do not wipe it.

    ``on_progress`` is an optional ``callable(str)`` for UI task messages.
    """
    proxmox = get_proxmox_api()
    node = _get_vm_node(vmid)
    if not node:
        raise Exception(f"VM {vmid} not found.")

    deadline = time.time() + timeout
    ok_accum = 0.0
    last_ok_at = None
    down_since = None
    last_progress = None
    drop_reset = max(poll, int(drop_reset))

    def _progress(msg):
        nonlocal last_progress
        if on_progress and msg != last_progress:
            last_progress = msg
            on_progress(msg)

    while time.time() < deadline:
        try:
            up = _guest_agent_is_up(proxmox, node, vmid)
        except Exception as e:
            up = False
            if ok_accum > 0 or last_ok_at is not None:
                logging.info(
                    f"Guest agent on VM {vmid} blip during stability wait "
                    f"(held {ok_accum:.0f}/{stable_for}s): {e}"
                )

        now = time.time()
        if up:
            down_since = None
            if last_ok_at is not None:
                ok_accum += now - last_ok_at
            last_ok_at = now
            if ok_accum >= stable_for:
                logging.info(
                    f"Guest agent on VM {vmid} accumulated {ok_accum:.0f}s uptime "
                    f"(need {stable_for}s)."
                )
                _progress(f"Guest agent stable ({int(ok_accum)}s).")
                return
            _progress(
                f"Waiting for guest agent stability "
                f"({int(ok_accum)}/{stable_for}s held)..."
            )
        else:
            last_ok_at = None
            if down_since is None:
                down_since = now
            down_for = now - down_since
            if down_for >= drop_reset and ok_accum:
                logging.info(
                    f"Guest agent on VM {vmid} down {down_for:.0f}s — "
                    f"resetting stability progress (was {ok_accum:.0f}s)."
                )
                ok_accum = 0.0
            _progress(
                f"Guest agent unavailable "
                f"(down {int(down_for)}s; held {int(ok_accum)}/{stable_for}s)..."
            )
        time.sleep(poll)

    raise Exception(
        f"Timed out waiting for a stable QEMU Guest Agent on VM {vmid} "
        f"(timeout={timeout}s, stable_for={stable_for}s, held={ok_accum:.0f}s)."
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


def _guest_agent_is_up(proxmox, node, vmid):
    """Cheap reachability check (prefer ping over get-fsinfo)."""
    agent = proxmox.nodes(node).qemu(vmid).agent
    try:
        agent.ping.post()
        return True
    except Exception:
        pass
    try:
        return agent.get('get-fsinfo') is not None
    except Exception:
        return False


# PVE ``agent/file-write`` content maxLength is ~60 KiB; stay under with margin.
_FILE_WRITE_MAX = 45 * 1024


def _ensure_guest_parent_dir(vmid, file_path):
    """Create the parent directory for ``file_path`` inside the guest."""
    run_command_in_guest(
        vmid,
        "powershell -NoProfile -Command \""
        f"New-Item -ItemType Directory -Force -Path (Split-Path -Path '{file_path}' -Parent) "
        f"| Out-Null\"",
    )


def _agent_file_write_once(vmid, raw, file_path):
    """Single attempt at native QGA file-write (no guest-exec payload)."""
    proxmox = get_proxmox_api()
    node = _get_vm_node(vmid)
    if not node:
        raise Exception(f"VM {vmid} not found.")
    content_b64 = base64.b64encode(raw).decode('ascii')
    endpoint = proxmox.nodes(node).qemu(vmid).agent('file-write')
    try:
        # encode=0: content is already base64 (required for arbitrary binary).
        endpoint.post(file=file_path, content=content_b64, encode=0)
        return
    except Exception as e:
        msg = str(e).lower()
        # PVE before encode= support, or ACL/param rejection — try auto-encode.
        if _is_transient_agent_error(e):
            raise
        if 'encode' not in msg and '400' not in msg and 'parameter' not in msg:
            raise
        logging.info(
            "VM %s: agent file-write encode=0 unsupported (%s); retrying with auto-encode",
            vmid,
            e,
        )
    # latin-1 round-trip preserves all byte values as code points for encode=1.
    endpoint.post(file=file_path, content=raw.decode('latin-1'))


def _agent_file_write(vmid, raw, file_path, retries=8, retry_delay=15):
    """Write via ``agent/file-write`` with transient-agent retries."""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            _agent_file_write_once(vmid, raw, file_path)
            return
        except Exception as e:
            last_err = e
            if not _is_transient_agent_error(e):
                raise
            logging.warning(
                f"Guest agent file-write failed on VM {vmid} "
                f"(attempt {attempt}/{retries}): {e}"
            )
            if attempt == retries:
                break
            try:
                wait_for_guest_agent(
                    vmid,
                    timeout=max(90, retry_delay * 3),
                    stable_for=8,
                    poll=8,
                    drop_reset=24,
                )
            except Exception as wait_err:
                logging.warning(f"Re-wait for guest agent on VM {vmid}: {wait_err}")
                time.sleep(retry_delay)
    raise last_err


def _is_share_violation(exc):
    """True when the guest cannot overwrite a file because another process holds it."""
    msg = str(exc).lower()
    return (
        'being used by another process' in msg
        or 'cannot access the file' in msg
        or 'sharing violation' in msg
    )


def _unlock_guest_path(vmid, file_path):
    """Drop leftover GuestOS-Setup / PowerShell that may be locking ``file_path``.

    Win11 templates that were themselves customized keep GuestOS-Setup and
    ``GuestOS-RegisterSetup.ps1``. After clone boot the leftover task can still
    be running ``-File`` on that script when we try to overwrite it.
    """
    leaf = file_path.replace('/', '\\').rsplit('\\', 1)[-1]
    run_command_in_guest(
        vmid,
        "powershell -NoProfile -Command \""
        "$ErrorActionPreference='SilentlyContinue'; "
        "Unregister-ScheduledTask -TaskName 'GuestOS-Setup' -Confirm:$false; "
        f"$leaf='{leaf}'; "
        "Get-CimInstance Win32_Process | Where-Object { "
        "  $_.Name -match 'powershell|pwsh' -and $_.CommandLine "
        "  -and $_.CommandLine -like ('*'+$leaf+'*') "
        "} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; "
        "Start-Sleep -Seconds 1; "
        "exit 0\"",
        retries=2,
        retry_delay=3,
    )


def _write_file_to_guest_via_exec_once(vmid, raw, file_path):
    """Fallback: write via PowerShell guest-exec (inline or chunked base64)."""
    content_b64 = base64.b64encode(raw).decode('ascii')
    max_inline_b64 = 8000

    if len(content_b64) <= max_inline_b64:
        command = (
            "powershell -NoProfile -Command \""
            f"New-Item -ItemType Directory -Force -Path (Split-Path -Path '{file_path}' -Parent) | Out-Null; "
            f"[System.IO.File]::WriteAllBytes('{file_path}', "
            f"[System.Convert]::FromBase64String('{content_b64}'))\""
        )
        run_command_in_guest(vmid, command)
        return

    chunk_size = 6000
    b64_path = f'{file_path}.guestos.b64'
    run_command_in_guest(
        vmid,
        "powershell -NoProfile -Command \""
        f"New-Item -ItemType Directory -Force -Path (Split-Path -Path '{file_path}' -Parent) | Out-Null; "
        f"Set-Content -LiteralPath '{b64_path}' -Value '' -Encoding Ascii\"",
    )
    for i in range(0, len(content_b64), chunk_size):
        chunk = content_b64[i:i + chunk_size]
        run_command_in_guest(
            vmid,
            "powershell -NoProfile -Command \""
            f"Add-Content -LiteralPath '{b64_path}' -Value '{chunk}' "
            f"-Encoding Ascii -NoNewline\"",
        )
    run_command_in_guest(
        vmid,
        "powershell -NoProfile -Command \""
        f"$b = Get-Content -LiteralPath '{b64_path}' -Raw; "
        f"[System.IO.File]::WriteAllBytes('{file_path}', "
        f"[System.Convert]::FromBase64String($b)); "
        f"Remove-Item -LiteralPath '{b64_path}' -Force -ErrorAction SilentlyContinue\"",
    )


def _write_file_to_guest_via_exec(vmid, raw, file_path):
    """Fallback write with retries when the destination is locked."""
    last_err = None
    for attempt in range(1, 6):
        try:
            _write_file_to_guest_via_exec_once(vmid, raw, file_path)
            return
        except Exception as e:
            last_err = e
            if not _is_share_violation(e) or attempt == 5:
                raise
            logging.warning(
                'VM %s: guest file %s locked (attempt %s/5): %s',
                vmid, file_path, attempt, e,
            )
            try:
                _unlock_guest_path(vmid, file_path)
            except Exception as unlock_err:
                logging.warning(
                    'VM %s: unlock %s failed: %s', vmid, file_path, unlock_err,
                )
            time.sleep(2 * attempt)
    raise last_err


def write_file_to_guest(vmid, content, file_path):
    """Write a file into the guest via QEMU Guest Agent.

    Prefers native Proxmox ``agent/file-write`` (one QGA file transfer) over
    embedding base64 in ``guest-exec`` PowerShell — large ``setup.ps1`` payloads
    previously needed many Add-Content execs and overloaded the agent so the
    follow-up ReadAllBytes could not find the staged file.
    """
    raw = content if isinstance(content, (bytes, bytearray)) else bytes(content)
    _ensure_guest_parent_dir(vmid, file_path)

    try:
        if len(raw) <= _FILE_WRITE_MAX:
            _agent_file_write(vmid, raw, file_path)
            return

        # Oversized: write binary parts via file-write, then one small join exec.
        part_paths = []
        try:
            for i in range(0, len(raw), _FILE_WRITE_MAX):
                part = f'{file_path}.guestos.part{i // _FILE_WRITE_MAX}'
                part_paths.append(part)
                _agent_file_write(vmid, raw[i:i + _FILE_WRITE_MAX], part)
            joined = ','.join(f"'{p}'" for p in part_paths)
            run_command_in_guest(
                vmid,
                "powershell -NoProfile -Command \""
                "$ErrorActionPreference='Stop'; "
                f"$parts=@({joined}); "
                "$out=@(); foreach($p in $parts){ $out += [IO.File]::ReadAllBytes($p) }; "
                f"[IO.File]::WriteAllBytes('{file_path}', [byte[]]$out); "
                "foreach($p in $parts){ Remove-Item -LiteralPath $p -Force "
                "-ErrorAction SilentlyContinue }\""
            )
            return
        except Exception as e:
            logging.warning(
                "VM %s: chunked agent file-write failed (%s); falling back to exec",
                vmid,
                e,
            )
    except Exception as e:
        if _is_transient_agent_error(e):
            raise
        logging.warning(
            "VM %s: agent file-write unavailable (%s); falling back to exec",
            vmid,
            e,
        )

    _write_file_to_guest_via_exec(vmid, raw, file_path)


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
                # Keep re-waits light: ping + longer poll, short stability window.
                wait_for_guest_agent(
                    vmid,
                    timeout=max(90, retry_delay * 3),
                    stable_for=8,
                    poll=8,
                    drop_reset=24,
                )
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

