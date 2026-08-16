"""Tests for guest file write (native file-write + exec fallback)."""
from __future__ import annotations

import base64

from app import proxmox as proxmox_mod
from app.proxmox import write_file_to_guest


def test_write_file_to_guest_uses_native_file_write(monkeypatch):
    calls = {'mkdir': [], 'file_write': [], 'exec': []}

    monkeypatch.setattr(
        proxmox_mod,
        '_ensure_guest_parent_dir',
        lambda vmid, path: calls['mkdir'].append(path),
    )

    def _fw(vmid, raw, file_path, **kwargs):
        calls['file_write'].append((file_path, bytes(raw)))

    monkeypatch.setattr(proxmox_mod, '_agent_file_write', _fw)
    monkeypatch.setattr(
        proxmox_mod,
        'run_command_in_guest',
        lambda *a, **k: calls['exec'].append(a),
    )

    payload = b'hello-guestos'
    path = r'C:\ProgramData\GuestOS\setup.ps1'
    write_file_to_guest(42, payload, path)
    assert calls['mkdir'] == [path]
    assert calls['file_write'] == [(path, payload)]
    assert calls['exec'] == []


def test_write_file_to_guest_falls_back_to_exec(monkeypatch):
    calls = []

    monkeypatch.setattr(proxmox_mod, '_ensure_guest_parent_dir', lambda *a, **k: None)

    def _fw(*a, **k):
        raise Exception("Permission denied (file-write)")

    monkeypatch.setattr(proxmox_mod, '_agent_file_write', _fw)

    def _run(vmid, command, **kwargs):
        calls.append(command)
        return ''

    monkeypatch.setattr(proxmox_mod, 'run_command_in_guest', _run)
    payload = b'hello-guestos'
    write_file_to_guest(42, payload, r'C:\ProgramData\GuestOS\setup.ps1')
    assert len(calls) == 1
    assert base64.b64encode(payload).decode() in calls[0]
    assert 'WriteAllBytes' in calls[0]


def test_write_file_to_guest_chunks_large_payload_via_file_write(monkeypatch):
    writes = []
    execs = []

    monkeypatch.setattr(proxmox_mod, '_ensure_guest_parent_dir', lambda *a, **k: None)
    monkeypatch.setattr(proxmox_mod, '_FILE_WRITE_MAX', 100)

    def _fw(vmid, raw, file_path, **kwargs):
        writes.append((file_path, bytes(raw)))

    monkeypatch.setattr(proxmox_mod, '_agent_file_write', _fw)
    monkeypatch.setattr(
        proxmox_mod,
        'run_command_in_guest',
        lambda vmid, command, **k: execs.append(command),
    )

    payload = b'X' * 250
    path = r'C:\ProgramData\GuestOS\setup.ps1'
    write_file_to_guest(42, payload, path)
    assert len(writes) == 3
    assert all('.guestos.part' in p for p, _ in writes)
    assert len(execs) == 1
    assert 'WriteAllBytes' in execs[0]
    assert ''.join(d.decode() for _, d in writes) == payload.decode()


def test_write_file_to_guest_exec_fallback_chunks(monkeypatch):
    calls = []

    monkeypatch.setattr(proxmox_mod, '_ensure_guest_parent_dir', lambda *a, **k: None)

    def _fw(*a, **k):
        raise Exception("file-write disabled")

    monkeypatch.setattr(proxmox_mod, '_agent_file_write', _fw)
    monkeypatch.setattr(
        proxmox_mod,
        'run_command_in_guest',
        lambda vmid, command, **k: calls.append(command) or '',
    )

    payload = b'X' * 20000  # base64 ~26 KiB → above inline exec limit
    write_file_to_guest(42, payload, r'C:\ProgramData\GuestOS\setup.ps1')
    assert len(calls) >= 3
    assert any('Set-Content' in c and '.guestos.b64' in c for c in calls)
    assert any('Add-Content' in c for c in calls)
    full_b64 = base64.b64encode(payload).decode()
    assert not any(full_b64 in c for c in calls)


def test_write_file_to_guest_retries_share_violation(monkeypatch):
    calls = {'write': 0, 'unlock': 0}

    monkeypatch.setattr(proxmox_mod, '_ensure_guest_parent_dir', lambda *a, **k: None)

    def _fw(*a, **k):
        raise Exception('Permission denied (file-write)')

    def _once(vmid, raw, file_path):
        calls['write'] += 1
        if calls['write'] == 1:
            raise Exception(
                'Command failed with exit code 1: Exception calling "WriteAllBytes" '
                'with "2" argument(s): "The process cannot access the file '
                "'C:\\Windows\\System32\\GuestOS-RegisterSetup.ps1' because it is "
                'being used by another process."'
            )
        return None

    def _unlock(vmid, file_path):
        calls['unlock'] += 1

    monkeypatch.setattr(proxmox_mod, '_agent_file_write', _fw)
    monkeypatch.setattr(proxmox_mod, '_write_file_to_guest_via_exec_once', _once)
    monkeypatch.setattr(proxmox_mod, '_unlock_guest_path', _unlock)
    monkeypatch.setattr(proxmox_mod.time, 'sleep', lambda *a, **k: None)

    write_file_to_guest(42, b'hello-guestos', r'C:\Windows\System32\GuestOS-RegisterSetup.ps1')
    assert calls['write'] == 2
    assert calls['unlock'] == 1
