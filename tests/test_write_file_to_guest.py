"""Tests for guest file write chunking (qemu-ga command-line limits)."""
from __future__ import annotations

import base64

from app.proxmox import write_file_to_guest


def test_write_file_to_guest_inline_for_small_payload(monkeypatch):
    calls = []

    def _run(vmid, command, **kwargs):
        calls.append(command)
        return ''

    monkeypatch.setattr('app.proxmox.run_command_in_guest', _run)
    payload = b'hello-guestos'
    write_file_to_guest(42, payload, r'C:\ProgramData\GuestOS\setup.ps1')
    assert len(calls) == 1
    assert base64.b64encode(payload).decode() in calls[0]
    assert 'WriteAllBytes' in calls[0]
    assert 'Add-Content' not in calls[0]


def test_write_file_to_guest_chunks_large_payload(monkeypatch):
    calls = []

    def _run(vmid, command, **kwargs):
        calls.append(command)
        return ''

    monkeypatch.setattr('app.proxmox.run_command_in_guest', _run)
    payload = b'X' * 20000  # base64 ~26 KiB → above inline limit
    write_file_to_guest(42, payload, r'C:\ProgramData\GuestOS\setup.ps1')
    assert len(calls) >= 3  # truncate staging + >=1 chunk + decode
    assert any('Set-Content' in c and '.guestos.b64' in c for c in calls)
    assert any('Add-Content' in c for c in calls)
    assert any('FromBase64String($b)' in c for c in calls)
    # No single call embeds the entire payload.
    full_b64 = base64.b64encode(payload).decode()
    assert not any(full_b64 in c for c in calls)
