"""Tests for post-sysprep shutdown / reboot detection."""

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

    assert ca._wait_for_sysprep_shutdown(125, timeout=30, poll=1) == 'stopped'


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
        125, timeout=60, poll=1, agent_down_for=0,
    )
    assert outcome == 'running'


def test_wait_timeout_when_never_stops(monkeypatch):
    fake = _FakeProxmox()
    monkeypatch.setattr(sp, 'get_proxmox_api', lambda: fake)
    monkeypatch.setattr(sp, '_get_vm_node', lambda vmid: 'node1')
    monkeypatch.setattr(sp.time, 'sleep', lambda _s: None)

    # Freeze deadline by making time.time advance past timeout quickly.
    start = {'t': 1000.0}

    def _time():
        start['t'] += 20
        return start['t']

    monkeypatch.setattr(sp.time, 'time', _time)

    assert ca._wait_for_sysprep_shutdown(125, timeout=30, poll=1) is None


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

    assert ca._wait_for_vm_stopped(125, timeout=30) is False
