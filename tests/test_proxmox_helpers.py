import app.proxmox as pm


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


def test_windows_server_name_matcher():
    assert pm._looks_like_windows_server_name('WIN-SERVER2019-TPL') is True
    assert pm._looks_like_windows_server_name('Windows 2019 Core') is True
    assert pm._looks_like_windows_server_name('ws2019-base') is True
    assert pm._looks_like_windows_server_name('tpl-win-server2022') is True
    assert pm._looks_like_windows_server_name('WS2025-DC') is True
    assert pm._looks_like_windows_server_name('w2k22-base') is True
    assert pm._looks_like_windows_server_name('Windows11-template') is False


def test_is_windows_server_template_uses_vm_name(monkeypatch):
    vms = [{'vmid': 120, 'name': 'tpl-win-server2019', 'template': 1, 'node': 'pve'}]
    configs = {120: {'ostype': 'win10', 'name': 'ignored'}}
    monkeypatch.setattr(pm, 'get_proxmox_api', lambda: _FakeProxmoxTemplates(vms, configs))
    assert pm.is_windows_server_template(120) is True
    # Deprecated alias still works.
    assert pm.is_windows_server_2019_template(120) is True


def test_is_windows_server_template_2022_and_2025_names(monkeypatch):
    for vmid, name in ((121, 'tpl-server2022'), (122, 'win-server-2025-dc')):
        vms = [{'vmid': vmid, 'name': name, 'template': 1, 'node': 'pve'}]
        configs = {vmid: {'ostype': 'win11', 'name': 'ignored'}}
        monkeypatch.setattr(pm, 'get_proxmox_api', lambda v=vms, c=configs: _FakeProxmoxTemplates(v, c))
        assert pm.is_windows_server_template(vmid) is True
        assert pm.classify_windows_guest_family(vmid) == 'server'


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


def test_is_vmid_collision_error():
    assert pm._is_vmid_collision_error(
        '500 Internal Server Error: unable to create VM 124: config file already exists'
    )
    assert pm._is_vmid_collision_error('already exists on node pve')
    assert not pm._is_vmid_collision_error('clone storage full')


def test_clone_vm_retries_on_vmid_collision(monkeypatch):
    calls = {'nextid': 0, 'clone': 0}

    class _TaskStatus:
        def get(self):
            return {'status': 'stopped', 'exitstatus': 'OK'}

    class _Tasks:
        def __call__(self, _upid):
            return type('T', (), {'status': _TaskStatus()})()

    class _Config:
        def get(self):
            return {'net0': 'virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0', 'ostype': 'win11'}

        def post(self, **kwargs):
            return None

        def set(self, **kwargs):
            return None

    class _CloneEndpoint:
        def post(self, **params):
            calls['clone'] += 1
            if calls['clone'] == 1:
                raise Exception('500 unable to create VM 124: config file already exists')
            return 'UPID:pve:000:000:000:qmclone:125:root@pam:'

    class _QemuTemplate:
        def __init__(self):
            self.config = _Config()
            self.clone = _CloneEndpoint()

    class _QemuNew:
        def __init__(self):
            self.config = _Config()

    class _Node:
        def __init__(self):
            self.tasks = _Tasks()

        def qemu(self, vmid):
            if str(vmid) == '127':
                return _QemuTemplate()
            return _QemuNew()

    class _Nodes:
        def __call__(self, _node):
            return _Node()

    class _NextId:
        def get(self, reserve=None):
            calls['nextid'] += 1
            return 124 if calls['nextid'] == 1 else 125

    class _Resources:
        def get(self, type=None):
            return [{'vmid': 127, 'template': 1, 'node': 'pve', 'name': 'Win11'}]

    class _ClusterOk:
        def __init__(self):
            self.nextid = _NextId()
            self.resources = _Resources()

    class _Px:
        def __init__(self):
            self.nodes = _Nodes()
            self.cluster = _ClusterOk()

    monkeypatch.setattr(pm, 'get_proxmox_api', lambda: _Px())
    monkeypatch.setattr(pm, 'require_windows_guest', lambda *a, **k: 'win11')
    monkeypatch.setattr(pm, '_update_vm_tags', lambda *a, **k: (True, 'ok'))
    monkeypatch.setattr(pm.time, 'sleep', lambda *_a, **_k: None)

    result = pm.clone_vm(127, 'VDI-001', 2, 4096, 'vmbr0', None)
    assert result['vmid'] == 125
    assert calls['clone'] == 2
    assert calls['nextid'] == 2
