"""Tests for guest-agent stability wait (cumulative uptime + drop reset)."""
from __future__ import annotations

import pytest

from app import proxmox as proxmox_mod


class _FakeAgent:
    def __init__(self, results):
        self._results = list(results)
        self.calls = 0

    def get(self, _name):
        self.calls += 1
        if not self._results:
            raise Exception("QEMU guest agent is not running")
        item = self._results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def ping(self):
        return self

    def post(self):
        # Prefer ping in wait_for_guest_agent; reuse the same result queue.
        return self.get('ping')


class _FakeQemu:
    def __init__(self, agent):
        self.agent = agent


class _FakeNodes:
    def __init__(self, agent):
        self._agent = agent

    def qemu(self, _vmid):
        return _FakeQemu(self._agent)


class _FakeProxmox:
    def __init__(self, agent):
        self._nodes = _FakeNodes(agent)

    def nodes(self, _node):
        return self._nodes


def test_wait_for_guest_agent_accumulates_across_short_blips(monkeypatch):
    """Brief timeouts must not wipe stability progress."""
    # Pattern: up, up, blip, up, up... with poll=1 and stable_for=3.
    results = [
        {"ok": 1},
        {"ok": 1},
        Exception("got timeout"),
        {"ok": 1},
        {"ok": 1},
        {"ok": 1},
        {"ok": 1},
    ]
    agent = _FakeAgent(results)
    msgs = []

    monkeypatch.setattr(proxmox_mod, "get_proxmox_api", lambda: _FakeProxmox(agent))
    monkeypatch.setattr(proxmox_mod, "_get_vm_node", lambda vmid: "pve")

    sleeps = []

    def _sleep(sec):
        sleeps.append(sec)

    monkeypatch.setattr(proxmox_mod.time, "sleep", _sleep)

    # Freeze-ish clock: each loop advances 1s via side effect on sleep.
    clock = {"t": 1000.0}

    def _now():
        return clock["t"]

    real_sleep = _sleep

    def _sleep_advance(sec):
        real_sleep(sec)
        clock["t"] += sec

    monkeypatch.setattr(proxmox_mod.time, "time", _now)
    monkeypatch.setattr(proxmox_mod.time, "sleep", _sleep_advance)

    proxmox_mod.wait_for_guest_agent(
        42,
        timeout=30,
        stable_for=3,
        drop_reset=5,
        poll=1,
        on_progress=msgs.append,
    )
    assert agent.calls >= 4
    assert any("held" in m or "stable" in m for m in msgs)


def test_wait_for_guest_agent_resets_after_sustained_drop(monkeypatch):
    """Long outage (reboot) must reset accumulated stability."""
    # up enough to accumulate, then down longer than drop_reset, then up again.
    results = [
        {"ok": 1},
        {"ok": 1},
        {"ok": 1},
        Exception("not running"),
        Exception("not running"),
        Exception("not running"),
        Exception("not running"),
        Exception("not running"),
        Exception("not running"),
        {"ok": 1},
        {"ok": 1},
        {"ok": 1},
        {"ok": 1},
        {"ok": 1},
    ]
    agent = _FakeAgent(results)
    monkeypatch.setattr(proxmox_mod, "get_proxmox_api", lambda: _FakeProxmox(agent))
    monkeypatch.setattr(proxmox_mod, "_get_vm_node", lambda vmid: "pve")

    clock = {"t": 0.0}

    def _now():
        return clock["t"]

    def _sleep(sec):
        clock["t"] += sec

    monkeypatch.setattr(proxmox_mod.time, "time", _now)
    monkeypatch.setattr(proxmox_mod.time, "sleep", _sleep)

    proxmox_mod.wait_for_guest_agent(
        99,
        timeout=60,
        stable_for=3,
        drop_reset=3,
        poll=1,
    )
    # Must have continued after reset (more polls than a single 3s window).
    assert agent.calls >= 10
