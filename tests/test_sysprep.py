"""Tests for the sysprep template rendering and network validation.

These lock in the behaviour that the sysprep workflow actually applies the
hostname (unattend ComputerName) and static network config (setup.ps1), and
that untrusted values are validated before being rendered into the non-escaped
PowerShell script.
"""
import base64
import json

import pytest

from app import app as flask_app
from app.celery_app import (
    _validate_sysprep_network,
    _prepare_domain_join,
    _render_sysprep_files,
)
from app.validators import ValidationError


def _base_data():
    return {
        'hostname': 'WINSRV19-01',
        'timezone': 'Central European Standard Time',
        'administrator_password': 'Sup3r$ecret',
        'ip_address': '10.0.5.20',
        'netmask_cidr': '24',
        'gateway': '10.0.5.1',
        'dns_servers': '10.0.5.1, 8.8.8.8',
        'primary_mac_address': 'BC:24:11:AA:BB:CC',
    }


def test_validate_builds_dns_list_and_normalizes():
    data = _base_data()
    data['hostname'] = 'web-01.corp.local'
    _validate_sysprep_network(data)
    assert data['dns_list'] == ['10.0.5.1', '8.8.8.8']
    assert data['netmask_cidr'] == 24  # coerced to int
    assert data['hostname'] == 'web-01'  # first DNS label only


@pytest.mark.parametrize('field,value', [
    ('ip_address', '10.0.5.999'),
    ('ip_address', "1.2.3.4'; Remove-Item C:\\ #"),  # injection attempt
    ('gateway', 'not-an-ip'),
    ('netmask_cidr', '40'),
    ('dns_servers', '10.0.5.1, bogus'),
    ('primary_mac_address', 'ZZ:ZZ'),
])
def test_validate_rejects_bad_input(field, value):
    data = _base_data()
    data[field] = value
    with pytest.raises(ValidationError):
        _validate_sysprep_network(data)


def test_unattend_sets_hostname_and_timezone():
    data = _base_data()
    with flask_app.app_context():
        _validate_sysprep_network(data)
        xml, _ps1, _cmd = _render_sysprep_files(data)
    xml = xml.decode()
    assert '<ComputerName>WINSRV19-01</ComputerName>' in xml
    assert '<TimeZone>Central European Standard Time</TimeZone>' in xml
    assert '<InputLocale>en-US</InputLocale>' in xml
    assert 'GuestOS-RegisterSetup.cmd' in xml
    assert 'GuestOS-RunHidden.vbs' in xml
    assert 'wscript.exe //B //nologo' in xml
    assert 'register SYSTEM AtStartup setup task' in xml
    assert '<FirstLogonCommands>' not in xml
    assert '<AutoLogon>' not in xml
    assert 'GUESTOS_SETUP_B64' not in xml  # must NOT embed huge blob in unattend (hangs Sysprep)

    assert 'net user Administrator /active:yes' in xml
    assert 'Microsoft-Windows-Setup' not in xml
    assert '<WillShowUI>' not in xml
    assert '<ProductKey>' not in xml
    assert 'SetupDisplayedProductKey' not in xml  # VL / non-eval path
    # Client OOBE needs LocalAccounts when AutoLogon is absent (Server-only HideLocalAccountScreen).
    assert 'UnattendCreatedUser' in xml
    assert '<LocalAccounts>' in xml
    assert '<Name>GuestOSOobe</Name>' in xml
    assert '<HideOEMRegistrationScreen>true</HideOEMRegistrationScreen>' in xml
    assert 'EnableFirstLogonAnimation' in xml
    assert 'FilterAdministratorToken' in xml


def test_unattend_evaluation_skips_product_key_and_marks_oobe():
    data = _base_data()
    data['windows_evaluation'] = True
    data['product_key'] = 'N69G4-B89J2-4G8F4-WWYCC-J464C'  # must be ignored for Eval
    with flask_app.app_context():
        _validate_sysprep_network(data)
        xml, _ps1, _cmd = _render_sysprep_files(data)
    xml = xml.decode()
    assert '<ProductKey>' not in xml
    assert 'SetupDisplayedProductKey' in xml
    assert 'UnattendCreatedUser' in xml
    assert 'mark unattend-created user for OOBE' in xml
    assert 'GuestOS-RegisterSetup.cmd' in xml
    assert '<LocalAccounts>' in xml
    assert '<Name>GuestOSOobe</Name>' in xml
    assert '<AutoLogon>' not in xml
    assert '<FirstLogonCommands>' not in xml
    assert 'EnableFirstLogonAnimation' in xml
    assert 'FilterAdministratorToken' in xml


def test_unattend_includes_product_key_when_set():
    from app.windows_product_keys import _SERVER_GVLK

    key = _SERVER_GVLK[(2022, 'standard')]
    data = _base_data()
    data['product_key'] = key
    data['windows_evaluation'] = False
    with flask_app.app_context():
        _validate_sysprep_network(data)
        xml, _ps1, _cmd = _render_sysprep_files(data)
    xml = xml.decode()
    assert f'<ProductKey>{key}</ProductKey>' in xml
    assert 'Microsoft-Windows-Setup' not in xml


def test_validate_rejects_bad_product_key():
    data = _base_data()
    data['product_key'] = 'short'
    with pytest.raises(ValidationError):
        _validate_sysprep_network(data)


def test_ensure_server_product_key_injects_gvlk(monkeypatch):
    from app.sysprep_render import _ensure_server_product_key
    from app.windows_product_keys import _SERVER_GVLK

    monkeypatch.setattr('app.proxmox.is_windows_server_template', lambda *a, **k: True)
    monkeypatch.setattr(
        'app.sysprep_render._read_guest_windows_edition',
        lambda vmid: ('ServerStandard', 'Microsoft Windows Server 2022 Standard', 20348),
    )
    monkeypatch.setattr(
        'app.sysprep_render._guess_server_year_from_template',
        lambda vmid: 2022,
    )
    data = {'template_vmid': 130, 'product_key': ''}
    with flask_app.app_context():
        _ensure_server_product_key(data, 999)
    assert data['product_key'] == _SERVER_GVLK[(2022, 'standard')]
    assert data['windows_evaluation'] is False


def test_ensure_server_product_key_skips_eval_gvlk(monkeypatch):
    from app.sysprep_render import _ensure_server_product_key

    monkeypatch.setattr('app.proxmox.is_windows_server_template', lambda *a, **k: True)
    monkeypatch.setattr(
        'app.sysprep_render._read_guest_windows_edition',
        lambda vmid: (
            'ServerStandardEval',
            'Microsoft Windows Server 2019 Standard Evaluation',
            17763,
        ),
    )
    monkeypatch.setattr(
        'app.sysprep_render._guess_server_year_from_template',
        lambda vmid: 2019,
    )
    data = {'template_vmid': 120, 'product_key': ''}
    with flask_app.app_context():
        _ensure_server_product_key(data, 999)
    assert data['product_key'] == ''
    assert data['windows_evaluation'] is True


def test_ensure_server_product_key_skips_non_server(monkeypatch):
    from app.sysprep_render import _ensure_server_product_key

    monkeypatch.setattr('app.proxmox.is_windows_server_template', lambda *a, **k: False)
    data = {'template_vmid': 100, 'product_key': ''}
    with flask_app.app_context():
        _ensure_server_product_key(data, 999)
    assert data.get('product_key', '') == ''
    assert data['windows_evaluation'] is False


def test_ensure_server_product_key_skips_unknown_edition(monkeypatch):
    """Fail closed: empty edition must not invent a Standard GVLK."""
    from app.sysprep_render import _ensure_server_product_key

    monkeypatch.setattr('app.proxmox.is_windows_server_template', lambda *a, **k: True)
    monkeypatch.setattr(
        'app.sysprep_render._read_guest_windows_edition',
        lambda vmid: ('', '', 0),
    )
    monkeypatch.setattr(
        'app.sysprep_render._guess_server_year_from_template',
        lambda vmid: 2022,
    )
    data = {'template_vmid': 130, 'product_key': ''}
    with flask_app.app_context():
        _ensure_server_product_key(data, 999)
    assert data['product_key'] == ''
    assert data['windows_evaluation'] is False


def test_ensure_server_product_key_keeps_override(monkeypatch):
    from app.sysprep_render import _ensure_server_product_key
    from app.windows_product_keys import _SERVER_GVLK

    key = _SERVER_GVLK[(2022, 'datacenter')]

    monkeypatch.setattr('app.proxmox.is_windows_server_template', lambda *a, **k: True)
    monkeypatch.setattr(
        'app.sysprep_render._read_guest_windows_edition',
        lambda vmid: ('ServerDatacenter', 'x', 20348),
    )
    data = {'template_vmid': 130, 'product_key': key}
    with flask_app.app_context():
        _ensure_server_product_key(data, 999)
    assert data['product_key'] == key
    assert data['windows_evaluation'] is False


def test_unattend_uses_selected_locale():
    data = _base_data()
    data['locale'] = 'de-DE'
    with flask_app.app_context():
        _validate_sysprep_network(data)
        xml, _ps1, _cmd = _render_sysprep_files(data)
    xml = xml.decode()
    assert '<UILanguage>de-DE</UILanguage>' in xml
    assert '<SystemLocale>de-DE</SystemLocale>' in xml


def test_setup_ps1_contains_network_config():
    data = _base_data()
    with flask_app.app_context():
        _validate_sysprep_network(data)
        _xml, ps1, _cmd = _render_sysprep_files(data)
    ps1 = ps1.decode()
    assert 'nicsBlob' in ps1 or 'nics_b64' in ps1 or '$nicsBlob' in ps1
    blob_line = [ln for ln in ps1.splitlines() if '$nicsBlob' in ln][0]
    b64 = blob_line.split("'", 2)[1]
    nics = json.loads(base64.b64decode(b64))
    assert nics[0]['ip'] == '10.0.5.20'
    assert nics[0]['prefix'] == 24
    assert nics[0]['gateway'] == '10.0.5.1'
    assert nics[0]['dns'] == ['10.0.5.1', '8.8.8.8']
    assert nics[0]['mac'] == 'BC:24:11:AA:BB:CC'
    assert nics[0]['ipv6'] is False
    assert 'New-NetRoute' in ps1
    assert 'New-NetIPAddress' in ps1
    assert r"C:\ProgramData\GuestOS\setup.log" in ps1


def test_setup_ps1_resets_recycle_bin_on_data_volumes():
    data = _base_data()
    data['manage_disks'] = True
    data['disk_plan_b64'] = base64.b64encode(
        json.dumps(
            [
                {'role': 'os', 'serial': 'guestos-os'},
                {
                    'role': 'data',
                    'serial': 'guestos-data-0',
                    'drive_letter': 'D',
                    'label': 'Data',
                    'reformat': False,
                },
            ]
        ).encode('utf-8')
    ).decode('ascii')
    with flask_app.app_context():
        _validate_sysprep_network(data)
        _xml, ps1, _cmd = _render_sysprep_files(data)
    if isinstance(ps1, bytes):
        ps1 = ps1.decode()
    assert 'Reset-GuestOsRecycleBin' in ps1
    assert 'pending_reboot' in ps1
    assert 'Ensure-GuestOsSetupTask' in ps1
    assert 'Invoke-GuestOsHidden' in ps1
    assert 'Disable-GuestOsSetupTask' in ps1
    assert 'Wait-GuestOsNetworkReady' in ps1
    assert 'Keep-GuestOsAutoLogonForReboot' not in ps1
    assert 'GuestOSFinalizeSetup' not in ps1
    assert 'AutoAdminLogon' in ps1  # scrub leftover only
    assert 'pending_reboot observed -- marking setup done' in ps1
    assert 'Invoke-GuestOsSessionEnd' in ps1
    assert 'Invoke-GuestOsLogoff' not in ps1
    assert 'Removing stale pagefile' in ps1
    assert 'function Reset-GuestOsRecycleBin' in ps1
    assert "Remove-Item -LiteralPath $path -Recurse -Force" in ps1
    assert 'Reset-GuestOsRecycleBin -Letter $Letter' in ps1


def test_setup_complete_defers_to_scheduled_task():
    """SetupComplete must not run setup.ps1 (specialize cleanup drops ProgramData)."""
    data = _base_data()
    with flask_app.app_context():
        _validate_sysprep_network(data)
        _xml, _ps1, cmd = _render_sysprep_files(data)
    cmd = cmd.decode()
    assert 'deferring GuestOS setup.ps1 to GuestOS-Setup scheduled task' in cmd
    assert 'GuestOS-RegisterSetup.cmd' in cmd
    assert r'C:\ProgramData\GuestOS\setup.ps1' not in cmd
    assert 'powershell.exe -File' not in cmd


def test_setup_ps1_optional_ipv6():
    data = _base_data()
    data['enable_ipv6'] = True
    data['ipv6_address'] = '2001:db8::10'
    data['ipv6_prefix'] = 64
    data['ipv6_gateway'] = '2001:db8::1'
    with flask_app.app_context():
        _validate_sysprep_network(data)
        _xml, ps1, _cmd = _render_sysprep_files(data)
    assert data['enable_ipv6'] is True
    blob_line = [ln for ln in ps1.decode().splitlines() if '$nicsBlob' in ln][0]
    nics = json.loads(base64.b64decode(blob_line.split("'", 2)[1]))
    assert nics[0]['ipv6'] is True
    assert nics[0]['ip6'] == '2001:db8::10'
    assert nics[0]['prefix6'] == 64


def test_workgroup_when_not_joining_domain():
    data = _base_data()
    data['workgroup'] = 'LABNET'
    _prepare_domain_join(data)
    assert data['join_domain'] is False
    assert data['workgroup'] == 'LABNET'
    with flask_app.app_context():
        _validate_sysprep_network(data)
        _xml, ps1, _cmd = _render_sysprep_files(data)
    assert "Add-Computer -WorkGroupName $workgroup" in ps1.decode()


def test_setup_ps1_enables_admin_and_removes_other_local_users():
    data = _base_data()
    with flask_app.app_context():
        _validate_sysprep_network(data)
        _xml, ps1, _cmd = _render_sysprep_files(data)
    ps1 = ps1.decode()
    assert "Enable-LocalUser -Name 'Administrator'" in ps1
    assert 'Remove-LocalUser' in ps1
    assert 'keepLocalUsers' in ps1


def test_write_sysprep_files_registers_task_launcher(monkeypatch):
    """Host writes RegisterSetup + FirstLogon launcher before Sysprep."""
    from app import sysprep_render as sr

    written = []

    def _write(vmid, content, path):
        written.append(path)
        return None

    monkeypatch.setattr(sr, 'write_file_to_guest', _write)
    monkeypatch.setattr(
        sr,
        'run_command_in_guest',
        lambda *a, **k: None,
    )
    monkeypatch.setattr(sr, '_unlock_guest_path', lambda *a, **k: None)
    with flask_app.app_context():
        sr._write_sysprep_files(1, b'<xml/>', b'# ps1', b'@echo off')
    assert r'C:\Windows\System32\GuestOS-FirstLogon.cmd' in written
    assert r'C:\Windows\System32\GuestOS-RegisterSetup.cmd' in written
    assert r'C:\Windows\System32\GuestOS-RegisterSetup.ps1' in written
    assert r'C:\Windows\System32\GuestOS-RunHidden.vbs' in written
    assert r'C:\Windows\Setup\Scripts\SetupComplete.cmd' in written


def test_register_setup_ps1_has_startup_and_first_boot_triggers():
    """AtStartup alone is missed when registered during specialize; Once+repeat covers first boot."""
    from flask import render_template

    with flask_app.app_context():
        ps1 = render_template('sysprep/GuestOS-RegisterSetup.ps1')
    assert 'New-ScheduledTaskTrigger -AtStartup' in ps1
    assert 'RepetitionInterval' in ps1
    assert '-Once' in ps1
    assert 'Hours 24' in ps1
    assert 'first boot' in ps1.lower() or 'Once+2m' in ps1
    assert "Execute 'wscript.exe'" in ps1
    assert 'GuestOS-RunHidden.vbs' in ps1
    assert "Execute 'cmd.exe'" not in ps1


def test_static_allows_empty_gateway():
    """Secondary / multi-homed NICs often omit a default gateway on purpose."""
    data = _base_data()
    data['gateway'] = ''
    _validate_sysprep_network(data)
    assert data['gateway'] == ''
    assert data['nics'][0]['gateway'] == ''


def test_multi_nic_second_without_gateway():
    data = _base_data()
    data['nics'] = [
        {
            'network_mode': 'static',
            'ip_address': '10.0.5.20',
            'netmask_cidr': '24',
            'gateway': '10.0.5.1',
            'dns_servers': '10.0.5.1',
            'bridge': 'vmbr0',
        },
        {
            'network_mode': 'static',
            'ip_address': '10.0.9.20',
            'netmask_cidr': '24',
            'gateway': '',
            'dns_servers': '',
            'bridge': 'vmbr1',
        },
    ]
    with flask_app.app_context():
        _validate_sysprep_network(data)
        _xml, ps1, _cmd = _render_sysprep_files(data)
    assert data['nics'][0]['gateway'] == '10.0.5.1'
    assert data['nics'][1]['gateway'] == ''
    blob_line = [ln for ln in ps1.decode().splitlines() if '$nicsBlob' in ln][0]
    nics = json.loads(base64.b64decode(blob_line.split("'", 2)[1]))
    assert nics[1]['gateway'] == ''
    assert 'if ($gateway)' in ps1.decode()


# --- DHCP mode --------------------------------------------------------------

def test_dhcp_mode_skips_static_and_enables_dhcp():
    data = _base_data()
    data['network_mode'] = 'dhcp'
    # ip/gateway intentionally absent -> must NOT be required in DHCP mode.
    data.pop('ip_address'); data.pop('gateway')
    with flask_app.app_context():
        _validate_sysprep_network(data)
        assert data['use_dhcp'] is True
        _xml, ps1, _cmd = _render_sysprep_files(data)
    ps1 = ps1.decode()
    assert '-Dhcp Enabled' in ps1
    blob_line = [ln for ln in ps1.splitlines() if '$nicsBlob' in ln][0]
    nics = json.loads(base64.b64decode(blob_line.split("'", 2)[1]))
    assert nics[0]['dhcp'] is True
    assert 'ipconfig /renew' in ps1  # must re-acquire lease after clearing IPs


def test_dhcp_with_dns_override_sets_servers():
    data = _base_data()
    data['network_mode'] = 'dhcp'
    data['dns_servers'] = '10.0.0.9'
    with flask_app.app_context():
        _validate_sysprep_network(data)
        _xml, ps1, _cmd = _render_sysprep_files(data)
    ps1 = ps1.decode()
    blob_line = [ln for ln in ps1.splitlines() if '$nicsBlob' in ln][0]
    nics = json.loads(base64.b64decode(blob_line.split("'", 2)[1]))
    assert nics[0]['dns'] == ['10.0.0.9']
    assert 'Set-DnsClientServerAddress' in ps1


# --- Domain join ------------------------------------------------------------

def test_domain_join_packs_credentials_as_base64_json():
    data = _base_data()
    data.update(join_domain=True, domain_name='CORP.Example.com',
                domain_username='svc-join@corp.example.com', domain_password='p@ss',
                domain_ou='OU=Servers,DC=corp')
    _prepare_domain_join(data)
    assert data['join_domain'] is True
    assert data['domain_name'] == 'corp.example.com'  # normalized
    assert data['domain_username'] == 'svc-join@corp.example.com'
    assert 'domain_password' not in data  # raw secret scrubbed from payload
    decoded = json.loads(base64.b64decode(data['domain_join_b64']))
    assert decoded == {'domain': 'corp.example.com', 'username': 'svc-join@corp.example.com',
                       'password': 'p@ss', 'ou': 'OU=Servers,DC=corp'}


def test_domain_join_password_is_not_interpolated_raw():
    # A password with a single quote and $ would break naive interpolation; it
    # must only ever appear inside the Base64 blob, never as raw PowerShell.
    nasty = "a'; Remove-Item C:\\ #$x"
    data = _base_data()
    data.update(join_domain=True, domain_name='corp.local',
                domain_username=r'CORP\svc', domain_password=nasty)
    with flask_app.app_context():
        _validate_sysprep_network(data)
        _prepare_domain_join(data)
        _xml, ps1, _cmd = _render_sysprep_files(data)
    ps1 = ps1.decode()
    assert nasty not in ps1  # raw secret never appears
    assert 'Add-Computer' in ps1
    assert 'w32tm /resync /force' in ps1
    assert 'Format-DomainJoinError' in ps1
    assert 'not_an_ou' in ps1
    assert 'Write-GuestOsJoinDiag' in ps1
    assert r'C:\ProgramData\GuestOS\join-diag.txt' in ps1
    assert 'oupath=' in ps1
    assert 'CurrentBuild' in ps1
    # The blob still round-trips to the correct password.
    decoded = json.loads(base64.b64decode(data['domain_join_b64']))
    assert decoded['password'] == nasty


def test_unattend_odj_blob_renders_provisioning_only():
    """An ODJ blob joins during specialize with no credentials in the answer file."""
    data = _base_data()
    data.pop('ip_address', None)
    data.pop('gateway', None)
    data.update(
        network_mode='dhcp',
        use_dhcp=True,
        join_domain=True,
        domain_name='lab.test',
        domain_username='administrator@lab.test',
        domain_password='p@ss',
        domain_ou='OU=Computers,DC=lab,DC=test',
    )
    with flask_app.app_context():
        _validate_sysprep_network(data)
        _prepare_domain_join(data)
        data['odj_account_data'] = 'QUJDREVGRw=='
        xml, ps1, _cmd = _render_sysprep_files(data)
    xml = xml.decode()
    ps1 = ps1.decode()
    assert 'Microsoft-Windows-UnattendedJoin' in xml
    assert '<AccountData>QUJDREVGRw==</AccountData>' in xml
    ident = xml.split('<Identification>', 1)[1].split('</Identification>', 1)[0]
    assert '<Credentials>' not in ident
    assert '<JoinDomain>' not in ident
    assert 'p@ss' not in xml
    # setup.ps1 still carries the fallback, skips Add-Computer when already
    # joined, then reboots once so first interactive logon is a clean session.
    assert 'Add-Computer' in ps1
    assert 'Already domain-joined' in ps1
    assert 'join-path method=odj' in ps1
    assert "SetupJoinMethod -Value 'odj'" in ps1
    assert "SetupJoinMethod -Value 'add-computer'" in ps1
    assert 'Restarting so first interactive logon is a clean session.' in ps1
    assert 'Domain membership already in place; marking setup done.' not in ps1
    # The blob holds the machine account password; it must not linger in data.
    assert 'odj_account_data' not in data


def test_unattend_without_odj_blob_has_no_join_component():
    """Without a blob the component is omitted so specialize cannot stall."""
    for mode in ('dhcp', 'static'):
        data = _base_data()
        if mode == 'dhcp':
            data.pop('ip_address', None)
            data.pop('gateway', None)
            data.update(network_mode='dhcp', use_dhcp=True)
        data.update(
            join_domain=True,
            domain_name='lab.test',
            domain_username='administrator@lab.test',
            domain_password='p@ss',
        )
        with flask_app.app_context():
            _validate_sysprep_network(data)
            _prepare_domain_join(data)
            xml, ps1, _cmd = _render_sysprep_files(data)
        xml = xml.decode()
        ps1 = ps1.decode()
        assert 'Microsoft-Windows-UnattendedJoin' not in xml, mode
        assert 'Add-Computer' in ps1
        assert 'Already domain-joined' in ps1
        assert 'Restarting so first interactive logon is a clean session.' in ps1


def test_domain_join_rejects_bare_username():
    data = _base_data()
    data.update(join_domain=True, domain_name='corp.local',
                domain_username='svc-join', domain_password='p')
    with pytest.raises(ValidationError, match='bare names'):
        _prepare_domain_join(data)


def test_domain_join_requires_credentials():
    data = _base_data()
    data.update(join_domain=True, domain_name='corp.local', domain_username='', domain_password='')
    with pytest.raises(ValidationError):
        _prepare_domain_join(data)


@pytest.mark.parametrize('bad', ['not_a_domain', 'corp', '-bad.local', ''])
def test_domain_join_rejects_bad_domain(bad):
    data = _base_data()
    data.update(join_domain=True, domain_name=bad, domain_username='u@corp.local', domain_password='p')
    with pytest.raises(ValidationError):
        _prepare_domain_join(data)


def test_no_domain_join_leaves_flag_false():
    data = _base_data()
    _prepare_domain_join(data)  # join_domain not set
    assert data['join_domain'] is False
    assert 'domain_join_b64' not in data
