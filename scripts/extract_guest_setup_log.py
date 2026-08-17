#!/usr/bin/env python3
"""Pull C:/ProgramData/GuestOS/setup.log from a clone via QEMU Guest Agent.

Run on the GuestOS host (inside the web/worker container, which has PVE creds):

  docker exec -i -e TARGET=VDI-W11-TEST08 <web-container> python3 /app/scripts/extract_guest_setup_log.py

Or pass a VMID:

  docker exec -i -e TARGET=123 <web-container> python3 /app/scripts/extract_guest_setup_log.py

QGA paths use forward slashes. setup.log includes domain and join username
(not the password) — redact before sharing. After a failed join, also pull
join-diag.txt (OU, OS build, NetSetup hits) which is safe to share without
the join account password.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

SETUP_LOG = 'C:/ProgramData/GuestOS/setup.log'
JOIN_DIAG = 'C:/ProgramData/GuestOS/join-diag.txt'
NETSETUP = 'C:\\Windows\\Debug\\NetSetup.LOG'
MARKER_DIR = 'C:/ProgramData/GuestOS'


def _resolve_vmid(target: str) -> int:
    target = (target or '').strip()
    if not target:
        raise SystemExit('Set TARGET to a VMID or hostname (e.g. VDI-W11-TEST08).')
    if target.isdigit():
        return int(target)

    db = Path('/app/instance/site.db')
    if not db.is_file():
        cands = list(Path('/app/instance').glob('*.db')) if Path('/app/instance').is_dir() else []
        db = cands[0] if cands else db
    if not db.is_file():
        raise SystemExit(f'No SQLite DB to resolve hostname {target!r}; pass a VMID.')

    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    try:
        row = con.execute(
            'SELECT result_vmid, hostname, status FROM task '
            'WHERE hostname LIKE ? AND result_vmid IS NOT NULL '
            'ORDER BY updated_at DESC LIMIT 1',
            (f'%{target}%',),
        ).fetchone()
    finally:
        con.close()
    if not row or not row[0]:
        raise SystemExit(f'No result_vmid in job history for hostname like {target!r}.')
    print(f'resolved hostname={row[1]!r} status={row[2]} vmid={row[0]}', file=sys.stderr)
    return int(row[0])


def _ps(vmid: int, command: str) -> str:
    from app.proxmox import run_command_in_guest

    out = run_command_in_guest(
        vmid,
        ['powershell.exe', '-NoProfile', '-Command', command],
        retries=3,
        retry_delay=5,
    )
    return out if isinstance(out, str) else (out or b'').decode('utf-8', errors='replace')


def _try_file_read(vmid: int, path: str) -> str | None:
    """Native PVE agent/file-read when the ACL allows it."""
    import base64

    from app.proxmox import _get_vm_node, get_proxmox_api

    try:
        px = get_proxmox_api()
        node = _get_vm_node(vmid)
        if not node:
            return None
        data = px.nodes(node).qemu(vmid).agent('file-read').get(file=path)
    except Exception as e:  # noqa: BLE001
        print(f'file-read unavailable ({e}); using guest-exec', file=sys.stderr)
        return None
    if not isinstance(data, dict):
        return None
    content = data.get('content')
    if content is None:
        return None
    if isinstance(content, bytes):
        text = content.decode('utf-8', errors='replace')
    else:
        text = str(content)
        try:
            decoded = base64.b64decode(text, validate=True)
            if decoded and not text.startswith('setup.ps1'):
                text = decoded.decode('utf-8', errors='replace')
        except Exception:
            pass
    if data.get('truncated'):
        text += '\n[truncated by QGA file-read]\n'
    return text


def main() -> int:
    os.chdir('/app')
    target = os.environ.get('TARGET') or (sys.argv[1] if len(sys.argv) > 1 else '')
    tail = int(os.environ.get('SETUP_LOG_TAIL') or '400')

    from app import create_app

    app = create_app()
    with app.app_context():
        vmid = _resolve_vmid(target)
        print(f'=== markers vmid={vmid} ===')
        markers = _ps(
            vmid,
            "Get-ChildItem -LiteralPath '" + MARKER_DIR + "' -ErrorAction SilentlyContinue "
            "| ForEach-Object { $_.Name }; "
            "if (Test-Path -LiteralPath '" + MARKER_DIR + "/setup.done') { "
            "'--- setup.done ---'; Get-Content -LiteralPath '" + MARKER_DIR + "/setup.done' }; "
            "if (Test-Path -LiteralPath '" + MARKER_DIR + "/setup.failed') { "
            "'--- setup.failed ---'; Get-Content -LiteralPath '" + MARKER_DIR + "/setup.failed' }",
        )
        print(markers or '(no ProgramData\\GuestOS files)')

        print(f'=== {SETUP_LOG} (tail {tail}) ===')
        text = _try_file_read(vmid, SETUP_LOG)
        if text is None:
            text = _ps(
                vmid,
                "if (Test-Path -LiteralPath '" + SETUP_LOG + "') { "
                "Get-Content -LiteralPath '" + SETUP_LOG + "' -Tail " + str(tail) + " "
                "} else { 'MISSING: " + SETUP_LOG + "' }",
            )
        elif text.count('\n') > tail:
            lines = text.splitlines()
            text = '\n'.join(lines[-tail:]) + '\n'
        sys.stdout.write(text if text.endswith('\n') else text + '\n')

        print('=== join-related lines ===')
        joined = _ps(
            vmid,
            "if (Test-Path -LiteralPath '" + SETUP_LOG + "') { "
            "Select-String -LiteralPath '" + SETUP_LOG + "' "
            "-Pattern 'Domain join|join-path|join-diag|FAILED|invalid_credentials|unreachable|"
            "computer_account|permission_denied|not_an_ou|Add-Computer|already-joined|"
            "oupath|os caption' "
            "| ForEach-Object { $_.Line } "
            "} else { 'MISSING' }",
        )
        print(joined or '(no matching lines)')

        print(f'=== {JOIN_DIAG} ===')
        diag = _try_file_read(vmid, JOIN_DIAG)
        if diag is None:
            diag = _ps(
                vmid,
                "if (Test-Path -LiteralPath '" + JOIN_DIAG + "') { "
                "Get-Content -LiteralPath '" + JOIN_DIAG + "' "
                "} else { 'MISSING: " + JOIN_DIAG + "' }",
            )
        sys.stdout.write((diag if diag.endswith('\n') else diag + '\n') if diag else 'MISSING\n')

        print('=== NetSetup.LOG matching lines ===')
        net = _ps(
            vmid,
            "if (Test-Path -LiteralPath '" + NETSETUP + "') { "
            "Select-String -LiteralPath '" + NETSETUP + "' "
            "-Pattern 'is not an OU|CN=Computers|NetpGetComputerObjectDn|downlevel|0x2|"
            "Account does not exist|LDAP creation failed|NetpJoinDomain' "
            "| Select-Object -Last 40 | ForEach-Object { $_.Line } "
            "} else { 'MISSING: C:\\Windows\\Debug\\NetSetup.LOG' }",
        )
        print(net or '(no matching lines)')

        print('=== HKLM\\SOFTWARE\\GuestOS Setup* ===')
        reg = _ps(
            vmid,
            "Get-ItemProperty -LiteralPath 'HKLM:\\SOFTWARE\\GuestOS' -ErrorAction SilentlyContinue "
            "| Select-Object SetupStatus,SetupDetail,SetupJoinMethod,SetupJoinError,SetupUtc "
            "| Format-List",
        )
        print(reg or '(registry missing)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
