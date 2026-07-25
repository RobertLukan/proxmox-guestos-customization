"""Tests for post-sysprep verification messaging (no hang on missing DHCP)."""

import app.celery_app as ca


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


def test_verify_dhcp_reports_ip_not_assigned(monkeypatch):
    # Agent up, but only APIPA / loopback — no real lease.
    interfaces = {
        'result': [{
            'ip-addresses': [
                {'ip-address-type': 'ipv4', 'ip-address': '169.254.1.2'},
                {'ip-address-type': 'ipv4', 'ip-address': '127.0.0.1'},
            ],
        }],
    }
    monkeypatch.setattr(ca, 'get_proxmox_api', lambda: _FakeProxmox(interfaces))
    monkeypatch.setattr(ca, '_get_vm_node', lambda vmid: 'node1')
    monkeypatch.setattr(ca, 'run_command_in_guest', lambda *a, **k: 'LABTEST01\n')
    monkeypatch.setattr(ca.time, 'sleep', lambda _s: None)

    summary = ca._verify_sysprep_result(
        126, 'LABTEST01', expected_ip=None, timeout=15,
    )
    assert 'hostname=LABTEST01 (ok)' in summary
    assert 'IP not assigned (no DHCP lease detected)' in summary


def test_verify_static_reports_missing_expected_ip(monkeypatch):
    interfaces = {'result': [{'ip-addresses': []}]}
    monkeypatch.setattr(ca, 'get_proxmox_api', lambda: _FakeProxmox(interfaces))
    monkeypatch.setattr(ca, '_get_vm_node', lambda vmid: 'node1')
    monkeypatch.setattr(ca, 'run_command_in_guest', lambda *a, **k: 'LABTEST01\n')
    monkeypatch.setattr(ca.time, 'sleep', lambda _s: None)

    summary = ca._verify_sysprep_result(
        126, 'LABTEST01', expected_ip='10.0.0.50', timeout=15,
    )
    assert 'IP not assigned (expected 10.0.0.50 not visible)' in summary
