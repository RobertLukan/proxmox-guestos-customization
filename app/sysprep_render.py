"""Sysprep answer-file validation and guest file rendering."""
from __future__ import annotations

import base64
import json
import logging

from flask import render_template

from app.proxmox import _unlock_guest_path, run_command_in_guest, write_file_to_guest
from app.util import as_bool as _as_bool
from app.validators import (
    ValidationError,
    validate_bridge,
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
    nic['bridge'] = validate_bridge(nic.get('bridge'))
    nic['dns_list'] = validate_dns_servers(nic.get('dns_servers'), allow_ipv6=True)
    if not use_dhcp:
        nic['ip_address'] = validate_ipv4(nic.get('ip_address'), field=f'NIC{index} IP address')
        nic['netmask_cidr'] = validate_netmask(nic.get('netmask_cidr'))
        # Gateway is optional: secondary NICs often have no default route
        # (two default gateways is usually wrong).
        gw = (nic.get('gateway') or '').strip()
        nic['gateway'] = validate_ipv4(gw, field=f'NIC{index} gateway') if gw else ''
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

    from app.domain_credentials import prepare_join_credentials

    blob = prepare_join_credentials(data)
    data['join_domain'] = True
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

    Evaluation SKUs are left without a key — volume GVLKs are invalid there and
    break OOBE product-key install (blocking FirstLogonCommands). Sets
    ``windows_evaluation`` so unattend can take the Eval OOBE-skip branch.
    """
    from flask import current_app

    from app.proxmox import is_windows_server_template
    from app.windows_product_keys import is_evaluation_edition

    data['windows_evaluation'] = False

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

    data['windows_evaluation'] = is_evaluation_edition(edition_id, caption)

    if (data.get('product_key') or '').strip():
        return

    edition_known = bool((edition_id or '').strip() or (caption or '').strip())
    if not edition_known:
        # Fail closed: unknown edition must not get a VL Standard GVLK (that
        # recreates the Eval InstallPid 0xC004F015 regression).
        data['product_key'] = ''
        current_app.logger.warning(
            "Skipping auto-GVLK for VM %s: guest edition unknown "
            "(edition=%r caption=%r); pass product_key or fix guest agent WMI.",
            vmid,
            edition_id or '?',
            caption or '?',
        )
        return

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

    # Do not invent a Standard GVLK when the edition string did not match —
    # only inject a key we resolved from the live guest identity.
    data['product_key'] = key
    if key:
        current_app.logger.info(
            "Using Server GVLK for VM %s OOBE (edition=%r caption=%r build=%s)",
            vmid,
            edition_id or '?',
            caption or '?',
            build or '?',
        )
    elif data['windows_evaluation']:
        current_app.logger.info(
            "Skipping GVLK for Evaluation guest VM %s (edition=%r caption=%r); "
            "unattend will skip OOBE product-key UI.",
            vmid,
            edition_id or '?',
            caption or '?',
        )
    else:
        current_app.logger.warning(
            "No GVLK matched for VM %s (edition=%r caption=%r); "
            "OOBE may prompt for a product key unless product_key is set.",
            vmid,
            edition_id or '?',
            caption or '?',
        )


def _render_sysprep_files(data):
    """Render the three guest files from the (already validated) ``data``.

    Returns a tuple of (unattended_xml_bytes, setup_ps1_bytes,
    setup_complete_cmd_bytes).
    """
    # Template defaults — ``_ensure_server_product_key`` sets this for live jobs.
    data.setdefault('windows_evaluation', False)
    unattended_xml = render_template('sysprep/unattended.xml', **data).encode('utf-8')
    # The ODJ blob carries the machine account password; drop it after the
    # unattend render so Celery/task payloads do not keep a second copy.
    data.pop('odj_account_data', None)
    # UTF-8 BOM so Windows PowerShell 5.1 (-File) does not misread the script as
    # the system ANSI code page (which corrupts non-ASCII and breaks parsing).
    setup_ps1 = render_template('sysprep/setup.ps1', **data).encode('utf-8-sig')
    setup_complete = render_template('sysprep/SetupComplete.cmd', **data).encode('utf-8')
    return unattended_xml, setup_ps1, setup_complete


# Survives specialize; GuestOS-Setup (SYSTEM AtStartup) invokes this launcher.
_FIRSTLOGON_CMD = (
    '@echo off\r\n'
    'rem GuestOS: extract setup.ps1 from HKLM and run it (SYSTEM scheduled task).\r\n'
    'powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -Command '
    "\"$ErrorActionPreference='Stop'; "
    "$b=(Get-ItemProperty -Path 'HKLM:\\SOFTWARE\\GuestOS' -Name SetupPs1B64 -ErrorAction Stop).SetupPs1B64; "
    "$out=$env:TEMP+'\\GuestOS-setup.ps1'; "
    '[IO.File]::WriteAllBytes($out,[Convert]::FromBase64String($b)); '
    '& $out"\r\n'
)

_REGISTER_SETUP_CMD = (
    '@echo off\r\n'
    'REM Register GuestOS-Setup: SYSTEM AtStartup task that runs GuestOS-FirstLogon.cmd.\r\n'
    'powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File '
    '"%SystemRoot%\\System32\\GuestOS-RegisterSetup.ps1"\r\n'
    'exit /b %ERRORLEVEL%\r\n'
)

# Keep RunSynchronous Path short: inline `reg add` + wscript wrapper exceeded
# the specialize SMI limit (~259 chars → 0x80220005 "answer file is invalid").
_SPECIALIZE_CMD_HEAD = (
    '@echo off\r\n'
    'net user Administrator /active:yes\r\n'
    'reg add "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Setup\\OOBE" '
    '/v UnattendCreatedUser /t REG_DWORD /d 1 /f\r\n'
)
_SPECIALIZE_CMD_EVAL = (
    'reg add "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Setup\\OOBE" '
    '/v SetupDisplayedProductKey /t REG_DWORD /d 1 /f\r\n'
)
_SPECIALIZE_CMD_TAIL = (
    'reg add "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System" '
    '/v EnableFirstLogonAnimation /t REG_DWORD /d 0 /f\r\n'
    'reg add "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System" '
    '/v FilterAdministratorToken /t REG_DWORD /d 1 /f\r\n'
    'exit /b 0\r\n'
)


def _specialize_cmd_bytes(windows_evaluation: bool) -> bytes:
    body = _SPECIALIZE_CMD_HEAD
    if windows_evaluation:
        body += _SPECIALIZE_CMD_EVAL
    body += _SPECIALIZE_CMD_TAIL
    return body.encode('ascii')

# wscript.exe is a Windows-subsystem host (no console). Run style 0 hides cmd.
_RUN_HIDDEN_VBS = '\r\n'.join([
    "' GuestOS: run a command with no visible window and wait.",
    "' Usage: wscript.exe //B //nologo GuestOS-RunHidden.vbs <command> [args...]",
    'Option Explicit',
    'Dim sh, cmd, i, a0, low',
    'If WScript.Arguments.Count < 1 Then WScript.Quit 1',
    'a0 = WScript.Arguments(0)',
    'low = LCase(a0)',
    'If WScript.Arguments.Count = 1 And (Right(low, 4) = ".cmd" Or Right(low, 4) = ".bat") Then',
    '  cmd = "cmd.exe /c """ & a0 & """"',
    'Else',
    '  cmd = a0',
    '  For i = 1 To WScript.Arguments.Count - 1',
    '    cmd = cmd & " " & WScript.Arguments(i)',
    '  Next',
    'End If',
    'Set sh = CreateObject("WScript.Shell")',
    'WScript.Quit sh.Run(cmd, 0, True)',
    '',
])


def _write_sysprep_files(
    vmid, unattended_xml, setup_ps1, setup_complete, *, data=None
):
    """Write the answer file and persist setup.ps1 for the SYSTEM setup task.

    Loose files under ``System32\\Sysprep`` / ``ProgramData\\GuestOS`` are often
    deleted by specialize cleanup (while ``unattended.xml`` itself survives).
    Embedding a large base64 blob *inside* the answer file also hung Sysprep in
    lab, so the durable copy is stored in ``HKLM\\SOFTWARE\\GuestOS\\SetupPs1B64``
    (written in-guest from a staged file to avoid guest-agent cmdline limits).
    """
    try:
        _unlock_guest_path(vmid, r'C:\Windows\System32\GuestOS-RegisterSetup.ps1')
    except Exception as e:
        logging.warning('VM %s: pre-write unlock of GuestOS launchers failed: %s', vmid, e)
    write_file_to_guest(vmid, unattended_xml, r'C:\Windows\System32\Sysprep\unattended.xml')
    # Launcher + task registration (specialize RunSynchronous calls RegisterSetup).
    write_file_to_guest(
        vmid,
        _FIRSTLOGON_CMD.encode('ascii'),
        r'C:\Windows\System32\GuestOS-FirstLogon.cmd',
    )
    write_file_to_guest(
        vmid,
        _REGISTER_SETUP_CMD.encode('ascii'),
        r'C:\Windows\System32\GuestOS-RegisterSetup.cmd',
    )
    write_file_to_guest(
        vmid,
        _specialize_cmd_bytes(_as_bool((data or {}).get('windows_evaluation'))),
        r'C:\Windows\System32\GuestOS-Specialize.cmd',
    )
    write_file_to_guest(
        vmid,
        _RUN_HIDDEN_VBS.encode('ascii'),
        r'C:\Windows\System32\GuestOS-RunHidden.vbs',
    )
    register_ps1 = render_template('sysprep/GuestOS-RegisterSetup.ps1').encode('utf-8-sig')
    write_file_to_guest(
        vmid,
        register_ps1,
        r'C:\Windows\System32\GuestOS-RegisterSetup.ps1',
    )
    staged = r'C:\Windows\Temp\GuestOS-setup.staged.ps1'
    last_err = None
    for attempt in range(1, 4):
        write_file_to_guest(vmid, setup_ps1, staged)
        try:
            run_command_in_guest(
                vmid,
                "powershell -NoProfile -Command \""
                "$ErrorActionPreference='Stop'; "
                f"$staged='{staged}'; "
                # Single quotes only: nested double quotes do not survive the
                # guest-agent command line, so the throw text came back mangled
                # and the retry below could not match on it.
                "if (-not (Test-Path -LiteralPath $staged)) { "
                "throw ('GuestOS staged setup.ps1 missing: ' + $staged) }; "
                "$bytes=[IO.File]::ReadAllBytes($staged); "
                "$b64=[Convert]::ToBase64String($bytes); "
                "New-Item -Path 'HKLM:\\SOFTWARE\\GuestOS' -Force | Out-Null; "
                "New-ItemProperty -Path 'HKLM:\\SOFTWARE\\GuestOS' -Name SetupPs1B64 "
                "-PropertyType String -Value $b64 -Force | Out-Null; "
                "New-Item -ItemType Directory -Force -Path 'C:\\GuestOS' | Out-Null; "
                "Copy-Item -LiteralPath $staged -Destination 'C:\\GuestOS\\setup.ps1' -Force; "
                "Remove-Item -LiteralPath $staged -Force -ErrorAction SilentlyContinue\""
            )
            last_err = None
            break
        except Exception as e:
            last_err = e
            msg = str(e)
            if (
                'staged setup.ps1 missing' in msg
                or 'Could not find file' in msg
                or 'FileNotFoundException' in msg
            ):
                logging.warning(
                    "VM %s: staged setup.ps1 missing after write "
                    "(attempt %s/3); rewriting via guest agent",
                    vmid,
                    attempt,
                )
                continue
            raise
    if last_err is not None:
        raise last_err
    try:
        write_file_to_guest(vmid, setup_complete, r'C:\Windows\Setup\Scripts\SetupComplete.cmd')
    except Exception as e:
        logging.warning(f"VM {vmid}: best-effort SetupComplete.cmd write failed: {e}")
