"""Tests for post-sysprep verification messaging (no hang on missing DHCP)."""

import app.celery_app as ca
import app.sysprep_verify as sv


class _Agent:
    def __init__(self, interfaces):
        self._interfaces = interfaces

    def get(self, what):
        if what == 'network-get-interfaces':
            return self._interfaces
        if what == 'get-fsinfo':
            return {'result': []}
        raise Exception(f'unexpected agent get {what}')


class _Qemu:
    def __init__(self, interfaces):
        self.agent = _Agent(interfaces)


class _Nodes:
    def __init__(self, interfaces):
        self._interfaces = interfaces

    def __call__(self, _node):
        return self

    def qemu(self, _vmid):
        return _Qemu(self._interfaces)


class _FakeProxmox:
    def __init__(self, interfaces):
        self.nodes = _Nodes(interfaces)


def _patch_verify_deps(monkeypatch, interfaces, cmd_fn=None, marker=('done', None)):
    monkeypatch.setattr(sv, 'get_proxmox_api', lambda: _FakeProxmox(interfaces))
    monkeypatch.setattr(sv, '_get_vm_node', lambda vmid: 'node1')
    monkeypatch.setattr(sv.time, 'sleep', lambda _s: None)
    monkeypatch.setattr(sv, '_guest_setup_marker', lambda _vmid: marker)
    if cmd_fn is not None:
        monkeypatch.setattr(sv, 'run_command_in_guest', cmd_fn)
    else:
        monkeypatch.setattr(sv, 'run_command_in_guest', lambda *a, **k: 'LABTEST01\n')


def test_verify_requires_setup_done(monkeypatch):
    interfaces = {
        'result': [{
            'ip-addresses': [
                {'ip-address-type': 'ipv4', 'ip-address': '10.0.0.9'},
            ],
        }],
    }
    _patch_verify_deps(monkeypatch, interfaces, marker=(None, None))
    summary, ok = ca._verify_sysprep_result(
        126, 'LABTEST01', expected_ip=None, timeout=15,
    )
    assert ok is False
    assert 'setup.done missing' in summary


def test_verify_fails_on_setup_failed(monkeypatch):
    interfaces = {'result': []}
    _patch_verify_deps(monkeypatch, interfaces, marker=('failed', 'disk boom'))
    summary, ok = ca._verify_sysprep_result(
        126, 'LABTEST01', expected_ip=None, timeout=15,
    )
    assert ok is False
    assert 'setup.ps1 failed' in summary
    assert 'disk boom' in summary


def test_verify_dhcp_reports_ip_not_assigned(monkeypatch):
    # Agent up, but only APIPA / loopback — no real lease after setup.done.
    interfaces = {
        'result': [{
            'ip-addresses': [
                {'ip-address-type': 'ipv4', 'ip-address': '169.254.1.2'},
                {'ip-address-type': 'ipv4', 'ip-address': '127.0.0.1'},
            ],
        }],
    }
    _patch_verify_deps(monkeypatch, interfaces)

    summary, ok = ca._verify_sysprep_result(
        126, 'LABTEST01', expected_ip=None, timeout=15,
    )
    assert ok is False  # after setup.done, missing DHCP lease is a failure
    assert 'hostname=LABTEST01 (ok)' in summary
    assert 'IP not assigned (no DHCP lease detected)' in summary
    assert 'setup.done=ok' in summary


def test_verify_static_reports_missing_expected_ip(monkeypatch):
    interfaces = {'result': [{'ip-addresses': []}]}
    _patch_verify_deps(monkeypatch, interfaces)

    summary, ok = ca._verify_sysprep_result(
        126, 'LABTEST01', expected_ip='10.0.0.50', timeout=15,
    )
    assert ok is False
    assert 'IP not assigned (expected 10.0.0.50 not visible)' in summary


def test_parse_domain_membership_json():
    domain, part = ca._parse_domain_membership(
        '{"Domain":"lab.test","PartOfDomain":true}'
    )
    assert domain == 'lab.test'
    assert part is True


def test_parse_domain_membership_wmic():
    domain, part = ca._parse_domain_membership(
        'Domain=lab.test\r\nPartOfDomain=TRUE\r\n'
    )
    assert domain == 'lab.test'
    assert part is True


def test_domains_match_netbios_vs_fqdn():
    assert ca._domains_match('LAB', 'lab.test')
    assert ca._domains_match('lab.test', 'lab.test')
    assert not ca._domains_match('other.test', 'lab.test')


def test_verify_domain_joined_via_powershell_json(monkeypatch):
    interfaces = {
        'result': [{
            'ip-addresses': [
                {'ip-address-type': 'ipv4', 'ip-address': '192.168.123.181'},
            ],
        }],
    }

    def _cmd(_vmid, command, **_kw):
        if 'hostname' in command:
            return 'WIn11ADTest\n'
        if 'ConvertTo-Json' in command or 'Win32_ComputerSystem' in command:
            return '{"Domain":"lab.test","PartOfDomain":true}\n'
        raise AssertionError(command)

    _patch_verify_deps(monkeypatch, interfaces, cmd_fn=_cmd)

    summary, ok = ca._verify_sysprep_result(
        123,
        'WIn11ADTest',
        expected_ip='192.168.123.181',
        expected_domain='lab.test',
        timeout=30,
    )
    assert ok is True
    assert 'domain[lab.test]: joined' in summary


def test_verify_domain_unknown_fails_when_expected(monkeypatch):
    interfaces = {
        'result': [{
            'ip-addresses': [
                {'ip-address-type': 'ipv4', 'ip-address': '10.0.0.5'},
            ],
        }],
    }

    def _cmd(_vmid, command, **_kw):
        if 'hostname' in command:
            return 'HOST1\n'
        raise Exception("Command failed with exit code 1: 'wmic' is not recognized")

    _patch_verify_deps(monkeypatch, interfaces, cmd_fn=_cmd)

    summary, ok = ca._verify_sysprep_result(
        1, 'HOST1', expected_ip='10.0.0.5', expected_domain='lab.test', timeout=30,
    )
    assert ok is False
    assert 'domain[lab.test]: unknown' in summary
