#!/usr/bin/env python3
"""Verify guest scrub of SetupPs1B64 / setup.ps1 copies on a result VM."""
from __future__ import annotations

import sys

from app import create_app
from app.proxmox import _get_vm_node, get_proxmox_api, run_command_in_guest


def _ps(vmid: int, command: str) -> str:
    out = run_command_in_guest(
        vmid,
        ['powershell.exe', '-NoProfile', '-Command', command],
    )
    return ' '.join(str(out or '').split())


def main() -> int:
    vmid = int(sys.argv[1] if len(sys.argv) > 1 else 146)
    app = create_app()
    with app.app_context():
        px = get_proxmox_api()
        node = _get_vm_node(vmid)
        st = px.nodes(node).qemu(vmid).status.current.get()
        print('vm', vmid, 'name=', st.get('name'), 'status=', st.get('status'))

        checks = {
            'SetupPs1B64_registry': (
                "try { "
                "$p = Get-ItemProperty -Path 'HKLM:\\SOFTWARE\\GuestOS' "
                "-Name SetupPs1B64 -ErrorAction Stop; "
                "Write-Output ('PRESENT len=' + [string]$p.SetupPs1B64.Length) "
                "} catch { Write-Output 'ABSENT' }"
            ),
            'C_GuestOS_setup_ps1': (
                "if (Test-Path -LiteralPath 'C:\\GuestOS\\setup.ps1') "
                "{ 'PRESENT' } else { 'ABSENT' }"
            ),
            'Temp_GuestOS_setup_ps1': (
                "$paths = @('C:\\Windows\\Temp\\GuestOS-setup.ps1', "
                "($env:TEMP + '\\GuestOS-setup.ps1')); "
                "$found = @(); "
                "foreach ($p in $paths) { "
                "if (Test-Path -LiteralPath $p) { $found += $p } }; "
                "if ($found.Count) { 'PRESENT ' + ($found -join ';') } "
                "else { 'ABSENT' }"
            ),
            'setup_done_marker': (
                "$paths = @('C:\\ProgramData\\GuestOS\\setup.done', "
                "'C:\\Windows\\GuestOS\\setup.done'); "
                "$found = @(); "
                "foreach ($p in $paths) { "
                "if (Test-Path -LiteralPath $p) { $found += $p } }; "
                "if ($found.Count) { 'PRESENT ' + ($found -join ';') } "
                "else { 'ABSENT' }"
            ),
            'GuestOS_key_names': (
                "try { "
                "$props = Get-ItemProperty 'HKLM:\\SOFTWARE\\GuestOS' "
                "-ErrorAction Stop; "
                "($props.PSObject.Properties | "
                "Where-Object { $_.Name -notlike 'PS*' } | "
                "ForEach-Object { $_.Name }) -join ',' "
                "} catch { 'NO_KEY' }"
            ),
        }

        results = {k: _ps(vmid, cmd) for k, cmd in checks.items()}
        for k, v in results.items():
            print(f'{k}: {v}')

        ok = (
            results['SetupPs1B64_registry'].startswith('ABSENT')
            and results['C_GuestOS_setup_ps1'] == 'ABSENT'
            and results['Temp_GuestOS_setup_ps1'].startswith('ABSENT')
            and results['setup_done_marker'].startswith('PRESENT')
        )
        print('SCRUB_VERIFY', 'PASS' if ok else 'FAIL')
        return 0 if ok else 2


if __name__ == '__main__':
    raise SystemExit(main())
