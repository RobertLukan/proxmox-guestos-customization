"""Tests for post-sysprep shutdown / reboot detection and Panther log probe."""

import pytest

import app.celery_app as ca
import app.sysprep_power as sp


class _StatusCurrent:
    def __init__(self, parent):
        self._parent = parent

    def get(self):
        return {'status': self._parent.status}


class _Status:
    def __init__(self, parent):
        self.current = _StatusCurrent(parent)


class _Agent:
    def __init__(self, parent):
        self._parent = parent

    def get(self, _what):
        if not self._parent.agent_up:
            raise Exception('QEMU guest agent is not running')
        return {'result': []}


class _Qemu:
    def __init__(self, parent):
        self.status = _Status(parent)
        self.agent = _Agent(parent)


class _Nodes:
    def __init__(self, parent):
        self._parent = parent

    def __call__(self, _node):
        return self

    def qemu(self, _vmid):
        return _Qemu(self._parent)


class _FakeProxmox:
    def __init__(self):
        self.status = 'running'
        self.agent_up = True
        self.nodes = _Nodes(self)


def test_wait_returns_stopped(monkeypatch):
    fake = _FakeProxmox()
    fake.status = 'stopped'
    monkeypatch.setattr(sp, 'get_proxmox_api', lambda: fake)
    monkeypatch.setattr(sp, '_get_vm_node', lambda vmid: 'node1')
    monkeypatch.setattr(sp.time, 'sleep', lambda _s: None)

    assert ca._wait_for_sysprep_shutdown(125, timeout=30, poll=1, log_probe_every=0) == 'stopped'


def test_wait_returns_running_after_missed_stop(monkeypatch):
    fake = _FakeProxmox()
    ticks = {'n': 0}

    def _sleep(_s):
        ticks['n'] += 1
        # Simulate: agent up → agent down for several polls → agent back, still running.
        if ticks['n'] < 2:
            fake.agent_up = True
            fake.status = 'running'
        elif ticks['n'] < 12:
            fake.agent_up = False
            fake.status = 'running'
        else:
            fake.agent_up = True
            fake.status = 'running'

    monkeypatch.setattr(sp, 'get_proxmox_api', lambda: fake)
    monkeypatch.setattr(sp, '_get_vm_node', lambda vmid: 'node1')
    monkeypatch.setattr(sp.time, 'sleep', _sleep)

    # agent_down_for=0 makes the sustained-outage flag trip as soon as agent drops;
    # the next poll with agent up returns 'running'.
    outcome = ca._wait_for_sysprep_shutdown(
        125, timeout=60, poll=1, agent_down_for=0, log_probe_every=0,
    )
    assert outcome == 'running'


def test_wait_timeout_when_never_stops(monkeypatch):
    fake = _FakeProxmox()
    monkeypatch.setattr(sp, 'get_proxmox_api', lambda: fake)
    monkeypatch.setattr(sp, '_get_vm_node', lambda vmid: 'node1')
    monkeypatch.setattr(sp.time, 'sleep', lambda _s: None)
    monkeypatch.setattr(sp, 'probe_sysprep_panther_failure', lambda _vmid: None)

    # Freeze deadline by making time.time advance past timeout quickly.
    start = {'t': 1000.0}

    def _time():
        start['t'] += 20
        return start['t']

    monkeypatch.setattr(sp.time, 'time', _time)

    assert ca._wait_for_sysprep_shutdown(125, timeout=30, poll=1, log_probe_every=0) is None


def test_wait_for_vm_stopped_ignores_reboot_path(monkeypatch):
    fake = _FakeProxmox()
    ticks = {'n': 0}

    def _sleep(_s):
        ticks['n'] += 1
        if ticks['n'] < 3:
            fake.agent_up = False
        else:
            fake.agent_up = True
        fake.status = 'running'

    start = {'t': 0.0}

    def _time():
        # Advance enough that allow_reboot path would trip, but stop-only must timeout.
        start['t'] += 50
        return start['t']

    monkeypatch.setattr(sp, 'get_proxmox_api', lambda: fake)
    monkeypatch.setattr(sp, '_get_vm_node', lambda vmid: 'node1')
    monkeypatch.setattr(sp.time, 'sleep', _sleep)
    monkeypatch.setattr(sp.time, 'time', _time)
    monkeypatch.setattr(sp, 'probe_sysprep_panther_failure', lambda _vmid: None)

    assert ca._wait_for_vm_stopped(125, timeout=30) is False


_COPILOT_SETUPACT = """
Info  [0x0f0080] SYSPRP ActionPlatform::LaunchModule: Executing method 'SysprepGeneralizeValidate' from C:\\Windows\\System32\\AppxSysprep.dll
Error [0x0f0082] SYSPRP ActionPlatform::LaunchModule: Failure occurred while executing 'SysprepGeneralizeValidate' from C:\\Windows\\System32\\AppxSysprep.dll; dwRet = 0x80073cf2
Error               Package Microsoft.Copilot_... is installed for a user, but not provisioned for all users. This package will not function properly in the sysprep image.
Error               Failed to remove apps for the current user
Error               Exit code of RemoveAllApps thread was 0x80073cf2.
Error [0x0f0070] SYSPRP runDllGetErrorString: Failed to get error string
Error in validating the actions from actionFiles\\Generalize.xml
"""


def test_match_sysprep_failure_copilot():
    hit = sp._match_sysprep_failure(_COPILOT_SETUPACT)
    assert hit is not None
    line, _pat = hit
    assert (
        'sysprep image' in line.lower()
        or 'RemoveAllApps' in line
        or 'SysprepGeneralizeValidate' in line
        or 'validating the actions' in line.lower()
    )


def test_match_sysprep_failure_clean_log():
    assert sp._match_sysprep_failure('Info SYSPRP starting generalize\nInfo OK') is None


def test_match_sysprep_failure_ignores_ie_provider_info_noise():
    """IE provider RegOpenKeyEx Info lines must not abort an in-progress Sysprep."""
    blob = (
        "2026-08-14 21:50:34, Info                  SYSPRP [IE sysprep provider] "
        "RegOpenKeyEx failed on 'Software\\Microsoft\\Internet Explorer\\User Preferences' "
        "(0x00000002)\n"
        "2026-08-14 21:50:34, Info                  SYSPRP [IE sysprep provider] "
        "RegOpenKeyEx on 'Software\\Microsoft\\Internet Explorer\\User Preferences' returned 2\n"
    )
    assert sp._match_sysprep_failure(blob) is None


def test_probe_sysprep_panther_failure_from_setupact(monkeypatch):
    def _tail(vmid, path, lines=80):
        if 'setuperr' in path.lower():
            return ''
        return _COPILOT_SETUPACT

    monkeypatch.setattr(sp, '_read_guest_log_tail', _tail)
    msg = sp.probe_sysprep_panther_failure(125)
    assert msg
    assert 'Sysprep failed in guest' in msg
    assert 'setupact.log' in msg


def test_wait_raises_on_panther_failure(monkeypatch):
    fake = _FakeProxmox()
    monkeypatch.setattr(sp, 'get_proxmox_api', lambda: fake)
    monkeypatch.setattr(sp, '_get_vm_node', lambda vmid: 'node1')
    monkeypatch.setattr(sp.time, 'sleep', lambda _s: None)
    monkeypatch.setattr(
        sp,
        'probe_sysprep_panther_failure',
        lambda _vmid: 'Sysprep failed in guest (setupact.log): Copilot package',
    )

    start = {'t': 1000.0}

    def _time():
        # Stay under timeout for a few iterations so probe can run.
        start['t'] += 1
        return start['t']

    monkeypatch.setattr(sp.time, 'time', _time)

    with pytest.raises(sp.SysprepGuestFailed) as ei:
        ca._wait_for_sysprep_shutdown(125, timeout=60, poll=1, log_probe_every=1)
    assert 'Copilot' in str(ei.value)
