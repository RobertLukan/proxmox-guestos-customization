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
    validate_mac,
    validate_netmask,
)

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


