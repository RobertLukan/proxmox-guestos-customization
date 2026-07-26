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


def test_is_windows_ostype():
    assert pm.is_windows_ostype('win11') is True
    assert pm.is_windows_ostype('WIN10') is True
    assert pm.is_windows_ostype('l26') is False
    assert pm.is_windows_ostype(None) is False
    assert pm.is_windows_ostype('') is False


class _FakeConfigGet:
    def __init__(self, cfg):
        self._cfg = cfg

    def get(self):
        return self._cfg


class _FakeQemuConfig:
    def __init__(self, configs):
        self._configs = configs
        self.config = None

    def __call__(self, vmid):
        self.config = _FakeConfigGet(self._configs[int(vmid)])
        return self


class _FakeNodeConfig:
    def __init__(self, configs):
        self._qemu = _FakeQemuConfig(configs)

    def qemu(self, vmid):
        return self._qemu(vmid)


class _FakeNodesConfig:
    def __init__(self, configs):
        self._node = _FakeNodeConfig(configs)

    def __call__(self, _node):
        return self._node


class _FakeClusterResources:
    def __init__(self, vms):
        self._vms = vms

    def get(self, type=None):
        return self._vms


class _FakeProxmoxTemplates:
    def __init__(self, vms, configs):
        self.cluster = type('C', (), {'resources': _FakeClusterResources(vms)})()
        self.nodes = _FakeNodesConfig(configs)


def test_get_template_vms_windows_only(monkeypatch):
    vms = [
        {'vmid': 100, 'name': 'win-tpl', 'template': 1, 'node': 'pve'},
        {'vmid': 101, 'name': 'linux-tpl', 'template': 1, 'node': 'pve'},
        {'vmid': 102, 'name': 'not-tpl', 'template': 0, 'node': 'pve'},
    ]
    configs = {
        100: {'ostype': 'win11'},
        101: {'ostype': 'l26'},
        102: {'ostype': 'win10'},
    }
    monkeypatch.setattr(pm, 'get_proxmox_api', lambda: _FakeProxmoxTemplates(vms, configs))
    out = pm.get_template_vms()
    assert [t['vmid'] for t in out] == [100]


def test_require_windows_guest_rejects_linux(monkeypatch):
    configs = {50: {'ostype': 'l26'}}
    monkeypatch.setattr(pm, 'get_proxmox_api', lambda: _FakeProxmoxTemplates([], configs))
    monkeypatch.setattr(pm, '_get_vm_node', lambda vmid: 'pve')
    try:
        pm.require_windows_guest(50)
        assert False, 'expected ValueError'
    except ValueError as e:
        assert 'not a Windows guest' in str(e)


def test_require_sysprep_existing_always_rejects(monkeypatch):
    try:
        pm.require_sysprep_existing_target(121)
        assert False, 'expected ValueError'
    except ValueError as e:
        assert 'disabled' in str(e).lower()


def test_require_sysprep_template_rejects_non_template(monkeypatch):
    vms = [{'vmid': 121, 'name': 'prod', 'template': 0, 'node': 'pve'}]
    configs = {121: {'ostype': 'win10'}}
    monkeypatch.setattr(pm, 'get_proxmox_api', lambda: _FakeProxmoxTemplates(vms, configs))
    try:
        pm.require_sysprep_template(121)
        assert False, 'expected ValueError'
    except ValueError as e:
        assert 'not a proxmox template' in str(e).lower()


def test_require_sysprep_template_accepts_windows_template(monkeypatch):
    vms = [{'vmid': 120, 'name': 'tpl', 'template': 1, 'node': 'pve'}]
    configs = {120: {'ostype': 'win10'}}
    monkeypatch.setattr(pm, 'get_proxmox_api', lambda: _FakeProxmoxTemplates(vms, configs))
    monkeypatch.setattr(pm, '_get_vm_node', lambda vmid: 'pve')
    assert pm.require_sysprep_template(120) == 'win10'


def test_delete_vm_stops_then_deletes(monkeypatch):
    calls = {'stop': 0, 'delete': 0}

    class _Postable:
        def __init__(self, key):
            self._key = key

        def post(self, **_kwargs):
            calls[self._key] += 1

    class _StatusCurrent:
        def get(self):
            if calls['stop'] == 0:
                return {'status': 'running'}
            return {'status': 'stopped'}

    class _Status:
        def __init__(self):
            self.current = _StatusCurrent()
            self.stop = _Postable('stop')
            self.shutdown = _Postable('stop')

    class _Qemu:
        def __init__(self):
            self.status = _Status()

        def delete(self, **params):
            calls['delete'] += 1
            assert params.get('purge') == 1
            return {'ok': 1}

    class _Node:
        def qemu(self, vmid):
            assert vmid == 9055
            return _Qemu()

    class _Nodes:
        def __call__(self, node):
            assert node == 'pve'
            return _Node()

    class _Px:
        def __init__(self):
            self.nodes = _Nodes()

    monkeypatch.setattr(pm, 'get_proxmox_api', lambda: _Px())
    monkeypatch.setattr(pm, '_get_vm_node', lambda vmid: 'pve')
    monkeypatch.setattr(pm.time, 'sleep', lambda *_a, **_k: None)
    pm.delete_vm(9055)
    assert calls['stop'] == 1
    assert calls['delete'] == 1
