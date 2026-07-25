import base64
import json
import re

import app.proxmox as pm


# --- run_ps_with_params: no user bytes in the script text ---

class _FakeSession:
    def __init__(self):
        self.script = None

    def run_ps(self, script):
        self.script = script

        class _R:
            status_code = 0
            std_out = b''
            std_err = b''

        return _R()


def test_run_ps_with_params_prevents_injection():
    session = _FakeSession()
    evil = "'; Remove-Item C:\\ -Recurse -Force #"
    pm.run_ps_with_params(session, 'Rename-Computer -NewName $p.hostname', {'hostname': evil})

    # The raw malicious string must never appear literally in the script.
    assert evil not in session.script
    # The script references the value via the $p object.
    assert '$p.hostname' in session.script
    # The value is recoverable from the Base64 blob.
    blob = re.search(r"FromBase64String\('([^']+)'\)", session.script).group(1)
    assert json.loads(base64.b64decode(blob))['hostname'] == evil


# --- select_winrm_ip ---

def _net(ips):
    return {'result': [{'ip-addresses': [
        {'ip-address-type': 'ipv4', 'ip-address': ip} for ip in ips
    ]}]}


class _FakeAgent:
    def __init__(self, data):
        self._data = data

    def get(self, _what):
        return self._data


class _FakeQemu:
    def __init__(self, data):
        self.agent = _FakeAgent(data)


class _FakeNodes:
    def __init__(self, data):
        self._data = data

    def __call__(self, _node):
        return self

    def qemu(self, _vmid):
        return _FakeQemu(self._data)


class _FakeProxmox:
    def __init__(self, data):
        self.nodes = _FakeNodes(data)


def test_select_winrm_ip_prefers_subnet(monkeypatch):
    data = _net(['10.0.0.5', '192.168.100.20', '127.0.0.1', '169.254.1.1'])
    monkeypatch.setattr(pm, 'get_proxmox_api', lambda: _FakeProxmox(data))
    monkeypatch.setattr(pm, '_get_vm_node', lambda vmid: 'node1')
    # WINRM_SUBNET is 192.168.100.0/24 (set in conftest).
    assert pm.select_winrm_ip(123) == '192.168.100.20'


def test_select_winrm_ip_skips_loopback_and_apipa_without_subnet(monkeypatch):
    data = _net(['127.0.0.1', '169.254.5.5', '10.0.0.9'])
    monkeypatch.setattr(pm, 'get_proxmox_api', lambda: _FakeProxmox(data))
    monkeypatch.setattr(pm, '_get_vm_node', lambda vmid: 'node1')
    monkeypatch.setitem(pm.app.config, 'WINRM_SUBNET', None)
    assert pm.select_winrm_ip(123) == '10.0.0.9'


def test_select_winrm_ip_none_when_no_proxmox(monkeypatch):
    monkeypatch.setattr(pm, 'get_proxmox_api', lambda: None)
    assert pm.select_winrm_ip(123) is None


# --- _update_vm_tags ---

class _FakeConfigEndpoint:
    def __init__(self, store):
        self._store = store

    def get(self):
        return {'tags': self._store['tags']}

    def set(self, tags=None):
        self._store['tags'] = tags


class _FakeQemuTags:
    def __init__(self, store):
        self.config = _FakeConfigEndpoint(store)


class _FakeNodesTags:
    def __init__(self, store):
        self._store = store

    def __call__(self, _node):
        return self

    def qemu(self, _vmid):
        return _FakeQemuTags(self._store)


class _FakeProxmoxTags:
    def __init__(self, store):
        self.nodes = _FakeNodesTags(store)


def test_update_vm_tags_replaces_lifecycle_and_keeps_others(monkeypatch):
    store = {'tags': 'uuid:abc,lifecycle-cloning'}
    monkeypatch.setattr(pm, 'get_proxmox_api', lambda: _FakeProxmoxTags(store))
    ok, _msg = pm._update_vm_tags(1, 'node1', tags_to_add=['lifecycle-ready'])
    assert ok is True
    tags = set(store['tags'].split(','))
    assert 'lifecycle-ready' in tags
    assert 'lifecycle-cloning' not in tags  # previous lifecycle tag replaced
    assert 'uuid:abc' in tags  # non-lifecycle tags preserved
