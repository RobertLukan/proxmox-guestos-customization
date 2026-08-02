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
    assert 'FirstLogonCommands' in xml
    assert r'C:\ProgramData\GuestOS\setup.ps1' in xml
    assert '<AutoLogon>' in xml
    assert 'net user Administrator /active:yes' in xml
    assert 'Microsoft-Windows-Setup' not in xml
    assert '<WillShowUI>' not in xml
    assert '<ProductKey>' not in xml


def test_unattend_includes_product_key_when_set():
    data = _base_data()
    data['product_key'] = 'VDYBN-27WPP-V4HQT-9VMD4-VMK7H'
    with flask_app.app_context():
        _validate_sysprep_network(data)
        xml, _ps1, _cmd = _render_sysprep_files(data)
    xml = xml.decode()
    assert '<ProductKey>VDYBN-27WPP-V4HQT-9VMD4-VMK7H</ProductKey>' in xml
    assert 'Microsoft-Windows-Setup' not in xml


def test_validate_rejects_bad_product_key():
    data = _base_data()
    data['product_key'] = 'short'
    with pytest.raises(ValidationError):
        _validate_sysprep_network(data)


def test_ensure_server_product_key_injects_gvlk(monkeypatch):
    from app.sysprep_render import _ensure_server_product_key

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
    assert data['product_key'] == 'VDYBN-27WPP-V4HQT-9VMD4-VMK7H'


def test_ensure_server_product_key_skips_non_server(monkeypatch):
    from app.sysprep_render import _ensure_server_product_key

    monkeypatch.setattr('app.proxmox.is_windows_server_template', lambda *a, **k: False)
    data = {'template_vmid': 100, 'product_key': ''}
    with flask_app.app_context():
        _ensure_server_product_key(data, 999)
    assert data['product_key'] == ''


def test_ensure_server_product_key_keeps_override(monkeypatch):
    from app.sysprep_render import _ensure_server_product_key

    called = {'read': 0}

    def _read(_vmid):
        called['read'] += 1
        return ('ServerDatacenter', 'x', 20348)

    monkeypatch.setattr('app.sysprep_render._read_guest_windows_edition', _read)
    data = {'template_vmid': 130, 'product_key': 'WX4NM-KYWYW-QJJR4-XV3QB-6VM33'}
    with flask_app.app_context():
        _ensure_server_product_key(data, 999)
    assert called['read'] == 0
    assert data['product_key'] == 'WX4NM-KYWYW-QJJR4-XV3QB-6VM33'


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


def test_setup_complete_defers_to_firstlogon():
    """SetupComplete must not run setup.ps1 (specialize cleanup drops ProgramData)."""
    data = _base_data()
    with flask_app.app_context():
        _validate_sysprep_network(data)
        _xml, _ps1, cmd = _render_sysprep_files(data)
    cmd = cmd.decode()
    assert 'deferring GuestOS setup.ps1 to FirstLogonCommands' in cmd
    assert 'powershell.exe' not in cmd
    assert r'C:\ProgramData\GuestOS\setup.ps1' not in cmd


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
                domain_username='svc-join', domain_password='p@ss', domain_ou='OU=Servers,DC=corp')
    _prepare_domain_join(data)
    assert data['join_domain'] is True
    assert data['domain_name'] == 'corp.example.com'  # normalized
    assert 'domain_password' not in data  # raw secret scrubbed from payload
    decoded = json.loads(base64.b64decode(data['domain_join_b64']))
    assert decoded == {'domain': 'corp.example.com', 'username': 'svc-join',
                       'password': 'p@ss', 'ou': 'OU=Servers,DC=corp'}


def test_domain_join_password_is_not_interpolated_raw():
    # A password with a single quote and $ would break naive interpolation; it
    # must only ever appear inside the Base64 blob, never as raw PowerShell.
    nasty = "a'; Remove-Item C:\\ #$x"
    data = _base_data()
    data.update(join_domain=True, domain_name='corp.local',
                domain_username='svc', domain_password=nasty)
    with flask_app.app_context():
        _validate_sysprep_network(data)
        _prepare_domain_join(data)
        _xml, ps1, _cmd = _render_sysprep_files(data)
    ps1 = ps1.decode()
    assert nasty not in ps1  # raw secret never appears
    assert 'Add-Computer' in ps1
    # The blob still round-trips to the correct password.
    decoded = json.loads(base64.b64decode(data['domain_join_b64']))
    assert decoded['password'] == nasty


def test_domain_join_requires_credentials():
    data = _base_data()
    data.update(join_domain=True, domain_name='corp.local', domain_username='', domain_password='')
    with pytest.raises(ValidationError):
        _prepare_domain_join(data)


@pytest.mark.parametrize('bad', ['not_a_domain', 'corp', '-bad.local', ''])
def test_domain_join_rejects_bad_domain(bad):
    data = _base_data()
    data.update(join_domain=True, domain_name=bad, domain_username='u', domain_password='p')
    with pytest.raises(ValidationError):
        _prepare_domain_join(data)


def test_no_domain_join_leaves_flag_false():
    data = _base_data()
    _prepare_domain_join(data)  # join_domain not set
    assert data['join_domain'] is False
    assert 'domain_join_b64' not in data
