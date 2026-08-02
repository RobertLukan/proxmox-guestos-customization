"""Sysprep answer-file validation and guest file rendering."""
from __future__ import annotations

import base64
import json

from flask import render_template

from app.proxmox import write_file_to_guest
from app.util import as_bool as _as_bool
from app.validators import (
    ValidationError,
    validate_dns_servers,
    validate_domain,
    validate_hostname,
    validate_ipv4,
    validate_ipv6,
    validate_ipv6_prefix,
    validate_locale,
    validate_mac,
    validate_netmask,
    validate_timezone,
    validate_vlan,
    validate_workgroup,
)
from app.windows_identity import (
    DEFAULT_LOCALE,
    DEFAULT_TIMEZONE,
    DEFAULT_WORKGROUP,
)
from app.windows_product_keys import resolve_server_product_key


def _normalize_nics_from_request(data):
    """Build a list of NIC dicts from ``nics`` or legacy single-NIC fields."""
    raw_nics = data.get('nics')
    if isinstance(raw_nics, list) and raw_nics:
        return raw_nics
    return [{
        'bridge': data.get('bridge'),
        'vlan': data.get('vlan'),
        'network_mode': data.get('network_mode') or 'static',
        'ip_address': data.get('ip_address'),
        'netmask_cidr': data.get('netmask_cidr'),
        'gateway': data.get('gateway'),
        'dns_servers': data.get('dns_servers'),
        'enable_ipv6': data.get('enable_ipv6'),
        'ipv6_address': data.get('ipv6_address'),
        'ipv6_prefix': data.get('ipv6_prefix'),
        'ipv6_gateway': data.get('ipv6_gateway'),
        'primary_mac_address': data.get('primary_mac_address'),
    }]


def _validate_one_nic(nic, index=0):
    """Validate and normalize one NIC dict. Mutates and returns nic."""
    use_dhcp = (str(nic.get('network_mode') or 'static').lower() == 'dhcp')
    nic['use_dhcp'] = use_dhcp
    nic['network_mode'] = 'dhcp' if use_dhcp else 'static'
    nic['vlan'] = validate_vlan(nic.get('vlan'))
    nic['bridge'] = (nic.get('bridge') or '').strip() or None
    nic['dns_list'] = validate_dns_servers(nic.get('dns_servers'), allow_ipv6=True)
    if not use_dhcp:
        nic['ip_address'] = validate_ipv4(nic.get('ip_address'), field=f'NIC{index} IP address')
        nic['netmask_cidr'] = validate_netmask(nic.get('netmask_cidr'))
        nic['gateway'] = validate_ipv4(nic.get('gateway'), field=f'NIC{index} gateway')
    else:
        nic['ip_address'] = ''
        nic['netmask_cidr'] = None
        nic['gateway'] = ''

    enable_ipv6 = _as_bool(nic.get('enable_ipv6'))
    nic['enable_ipv6'] = enable_ipv6
    if enable_ipv6:
        nic['ipv6_address'] = validate_ipv6(nic.get('ipv6_address'), field=f'NIC{index} IPv6 address')
        nic['ipv6_prefix'] = validate_ipv6_prefix(nic.get('ipv6_prefix') or 64)
        gw6 = (nic.get('ipv6_gateway') or '').strip()
        nic['ipv6_gateway'] = validate_ipv6(gw6, field=f'NIC{index} IPv6 gateway') if gw6 else ''
    else:
        nic['ipv6_address'] = ''
        nic['ipv6_prefix'] = None
        nic['ipv6_gateway'] = ''

    if nic.get('primary_mac_address'):
        nic['primary_mac_address'] = validate_mac(nic['primary_mac_address'])
    return nic


def _validate_sysprep_network(data):
    """Validate/normalize network + identity values before template render.

    Mutates ``data`` in place. Builds ``nics`` (validated list) and packs
    ``nics_b64`` for setup.ps1. Legacy single-NIC fields remain populated from
    NIC 0 for verify/back-compat.
    """
    if data.get('hostname'):
        data['hostname'] = validate_hostname(data['hostname'])

    data['timezone'] = validate_timezone(data.get('timezone') or DEFAULT_TIMEZONE)
    data['locale'] = validate_locale(data.get('locale') or DEFAULT_LOCALE)

    # Optional operator override; GVLK auto-fill happens later via guest agent.
    pk = (data.get('product_key') or '').strip()
    if pk:
        try:
            data['product_key'] = resolve_server_product_key(product_key=pk)
        except ValueError as e:
            raise ValidationError(str(e))
    else:
        data['product_key'] = ''

    nics = [_validate_one_nic(dict(n), i) for i, n in enumerate(_normalize_nics_from_request(data))]
    if len(nics) > 8:
        raise ValidationError('At most 8 NICs are supported per VM.')
    data['nics'] = nics

    # Back-compat primary fields from NIC 0.
    primary = nics[0]
    data['use_dhcp'] = primary['use_dhcp']
    data['network_mode'] = primary['network_mode']
    data['dns_list'] = primary['dns_list']
    data['ip_address'] = primary.get('ip_address') or ''
    data['netmask_cidr'] = primary.get('netmask_cidr')
    data['gateway'] = primary.get('gateway') or ''
    data['enable_ipv6'] = primary['enable_ipv6']
    data['ipv6_address'] = primary.get('ipv6_address') or ''
    data['ipv6_prefix'] = primary.get('ipv6_prefix')
    data['ipv6_gateway'] = primary.get('ipv6_gateway') or ''
    if primary.get('vlan') is not None:
        data['vlan'] = primary['vlan']
    if primary.get('bridge'):
        data['bridge'] = primary['bridge']
    if primary.get('primary_mac_address'):
        data['primary_mac_address'] = primary['primary_mac_address']

    # Compact blob for setup.ps1 (validated values only).
    blob_nics = []
    for nic in nics:
        blob_nics.append({
            'mac': nic.get('primary_mac_address') or '',
            'dhcp': bool(nic['use_dhcp']),
            'ip': nic.get('ip_address') or '',
            'prefix': nic.get('netmask_cidr'),
            'gateway': nic.get('gateway') or '',
            'dns': nic.get('dns_list') or [],
            'ipv6': bool(nic['enable_ipv6']),
            'ip6': nic.get('ipv6_address') or '',
            'prefix6': nic.get('ipv6_prefix'),
            'gw6': nic.get('ipv6_gateway') or '',
        })
    data['nics_b64'] = base64.b64encode(json.dumps(blob_nics).encode('utf-8')).decode('ascii')


def _prepare_domain_join(data):
    """Validate domain-join inputs and stage them for the setup.ps1 template.

    When a domain join is requested, credentials are packed into a Base64-encoded
    JSON blob (``domain_join_b64``) so no credential bytes are interpolated into
    PowerShell syntax. The raw password is removed from ``data`` afterwards so it
    does not linger in the task payload/logs. Raises ValidationError on bad input.

    When not joining a domain, validates optional ``workgroup`` (defaults to WORKGROUP).
    """
    if not _as_bool(data.get('join_domain')):
        data['join_domain'] = False
        data['workgroup'] = validate_workgroup(data.get('workgroup') or DEFAULT_WORKGROUP)
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
    data['workgroup'] = ''
    data['domain_join_b64'] = base64.b64encode(
        json.dumps(blob).encode('utf-8')
    ).decode('ascii')
    # Do not keep the raw secret around once it is packed into the blob.
    data.pop('domain_password', None)


def _guess_server_year_from_template(vmid):
    """Best-effort Server LTSC year from template name/tags (0 if unknown)."""
    from app.proxmox import _template_name_tags

    name, tags, _ostype = _template_name_tags(vmid)
    blob = f'{name or ""} {tags or ""}'.lower()
    for year in (2025, 2022, 2019, 2016):
        if str(year) in blob:
            return year
    return 0


def _read_guest_windows_edition(vmid):
    """Return ``(edition_id, caption, build)`` from the guest, best-effort."""
    import re

    from app.proxmox import run_command_in_guest

    ps = (
        "powershell.exe -NoProfile -Command "
        "\"$cv = Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion'; "
        "$os = Get-CimInstance Win32_OperatingSystem; "
        "Write-Output (($cv.EditionID) + '|' + ($os.Caption) + '|' + ($cv.CurrentBuild))\""
    )
    out = (run_command_in_guest(vmid, ps) or '').strip()
    parts = out.split('|')
    edition_id = (parts[0] if len(parts) > 0 else '').strip()
    caption = (parts[1] if len(parts) > 1 else '').strip()
    build = 0
    if len(parts) > 2:
        try:
            build = int(re.sub(r'\D', '', parts[2]) or 0)
        except ValueError:
            build = 0
    return edition_id, caption, build


def _ensure_server_product_key(data, vmid):
    """For Server templates, ensure specialize has a ProductKey.

    Explicit ``product_key`` wins. Otherwise inject the matching Microsoft GVLK
    so OOBE does not stop on Enter product key. (Unattend cannot click
    "Do this later"; empty Setup ProductKey/WillShowUI in specialize fails.)
    """
    from flask import current_app

    from app.proxmox import is_windows_server_template

    if (data.get('product_key') or '').strip():
        return

    template_vmid = data.get('template_vmid') or vmid
    if not is_windows_server_template(template_vmid):
        return

    edition_id, caption, build = '', '', 0
    try:
        edition_id, caption, build = _read_guest_windows_edition(vmid)
    except Exception as e:
        current_app.logger.warning(
            "Could not read Windows edition from VM %s: %s", vmid, e
        )

    default_year = _guess_server_year_from_template(template_vmid) or 2022
    try:
        key = resolve_server_product_key(
            edition_id=edition_id,
            caption=caption,
            build=build,
            default_year=default_year,
        )
    except ValueError:
        key = ''

    if not key:
        key = resolve_server_product_key(
            edition_id='ServerStandard',
            caption=f'Windows Server {default_year}',
            build=build,
            default_year=default_year,
        )
    data['product_key'] = key
    if key:
        current_app.logger.info(
            "Using Server GVLK for VM %s OOBE (edition=%r caption=%r build=%s)",
            vmid,
            edition_id or '?',
            caption or '?',
            build or '?',
        )


def _render_sysprep_files(data):
    """Render the three guest files from the (already validated) ``data``.

    Returns a tuple of (unattended_xml_bytes, setup_ps1_bytes,
    setup_complete_cmd_bytes).
    """
    unattended_xml = render_template('sysprep/unattended.xml', **data).encode('utf-8')
    # UTF-8 BOM so Windows PowerShell 5.1 (-File) does not misread the script as
    # the system ANSI code page (which corrupts non-ASCII and breaks parsing).
    setup_ps1 = render_template('sysprep/setup.ps1', **data).encode('utf-8-sig')
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
