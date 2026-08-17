"""Post-Sysprep verification via the QEMU guest agent."""
from __future__ import annotations

import json
import time

from app import app
from app.proxmox import (
    get_proxmox_api,
    _get_vm_node,
    run_command_in_guest,
    write_file_to_guest,
)

def _parse_domain_membership(raw):
    """Parse Domain / PartOfDomain from guest command output.

    Accepts WMIC ``/value`` lines, PowerShell ``ConvertTo-Json``, or a simple
    ``Domain\\tPartOfDomain`` line.
    """
    if not raw:
        return None, None
    text = raw.strip()
    domain = None
    part = None

    # JSON from ConvertTo-Json (single object).
    if text.startswith('{'):
        try:
            obj = json.loads(text)
            domain = obj.get('Domain') or obj.get('domain')
            part = obj.get('PartOfDomain')
            if part is None:
                part = obj.get('partOfDomain')
        except Exception:  # noqa: BLE001
            pass

    if domain is None or part is None:
        for line in text.replace('\r', '\n').split('\n'):
            line = line.strip()
            if not line or '=' not in line:
                continue
            key, _, val = line.partition('=')
            key = key.strip().lower()
            val = val.strip()
            if key == 'domain':
                domain = val
            elif key == 'partofdomain':
                part = val

    if part is not None and not isinstance(part, bool):
        part_s = str(part).strip().lower()
        if part_s in ('true', '1', 'yes'):
            part = True
        elif part_s in ('false', '0', 'no'):
            part = False
        else:
            part = None

    return domain, part


def _domains_match(actual, expected):
    """True if guest Domain matches expected FQDN/NetBIOS (case-insensitive)."""
    if not actual or not expected:
        return False
    a = str(actual).strip().lower().rstrip('.')
    e = str(expected).strip().lower().rstrip('.')
    if a == e:
        return True
    # Accept NetBIOS vs FQDN (LAB vs lab.test).
    if a.split('.')[0] == e.split('.')[0]:
        return True
    return False


def _read_domain_membership(vmid):
    """Query domain membership via guest agent (PowerShell CIM; no WMIC).

    WMIC is removed on modern Windows 11 images, so CIM/JSON is the primary path.
    Returns ``(domain_name_or_None, part_of_domain_or_None)``.
    """
    # Prefer JSON for reliable parsing.
    ps = (
        'powershell.exe -NoProfile -NonInteractive -Command '
        '"Get-CimInstance Win32_ComputerSystem | '
        'Select-Object Domain,PartOfDomain | ConvertTo-Json -Compress"'
    )
    try:
        out = run_command_in_guest(vmid, ps)
        domain, part = _parse_domain_membership(out)
        if domain is not None or part is not None:
            return domain, part
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"PowerShell domain query failed on VM {vmid}: {e}")

    # Fallback: legacy WMIC (Server 2019 / older images).
    try:
        out = run_command_in_guest(
            vmid, 'cmd.exe /c wmic computersystem get Domain,PartOfDomain /value')
        return _parse_domain_membership(out)
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"WMIC domain query failed on VM {vmid}: {e}")
        return None, None


def _guestos_reg_value(vmid, name):
    """Read HKLM\\SOFTWARE\\GuestOS\\<name> via QGA. Returns None if missing."""
    try:
        out = run_command_in_guest(
            vmid,
            f'cmd.exe /c reg query "HKLM\\SOFTWARE\\GuestOS" /v {name}',
        ) or ''
    except Exception as e:  # noqa: BLE001
        app.logger.info(f"VM {vmid} setup registry {name} read failed: {e}")
        return None
    for line in out.splitlines():
        if name in line and 'REG_' in line:
            if 'REG_SZ' in line:
                return line.split('REG_SZ', 1)[-1].strip()
            parts = line.split()
            return parts[-1].strip() if parts else None
    return None


def _read_setup_join_method(vmid):
    """Return 'odj' | 'add-computer' | None from the guest SetupJoinMethod key."""
    raw = (_guestos_reg_value(vmid, 'SetupJoinMethod') or '').strip().lower()
    if raw in ('odj', 'add-computer'):
        return raw
    return None


def _guest_setup_marker(vmid):
    """Return ('done'|'failed'|None, detail) from GuestOS markers.

    Prefer the durable registry key (survives ProgramData cleanup after a
    mistaken SetupComplete run), then fall back to file markers under
    ProgramData and ``C:\\Windows\\GuestOS``.
    """
    def _reg_value(name):
        return _guestos_reg_value(vmid, name)

    status = (_reg_value('SetupStatus') or '').lower()
    if status == 'failed':
        return 'failed', _reg_value('SetupDetail')
    if status == 'done':
        return 'done', _reg_value('SetupDetail') or 'ok'
    if status == 'pending_reboot':
        # Intentional pagefile/domain reboot — keep waiting for final done.
        return 'pending_reboot', _reg_value('SetupDetail') or 'pending'

    cmd = (
        'cmd.exe /c '
        'if exist C:\\ProgramData\\GuestOS\\setup.failed '
        '(echo FAILED& type C:\\ProgramData\\GuestOS\\setup.failed) '
        'else if exist C:\\Windows\\GuestOS\\setup.failed '
        '(echo FAILED& type C:\\Windows\\GuestOS\\setup.failed) '
        'else if exist C:\\ProgramData\\GuestOS\\setup.done '
        '(echo DONE& type C:\\ProgramData\\GuestOS\\setup.done) '
        'else if exist C:\\Windows\\GuestOS\\setup.done '
        '(echo DONE& type C:\\Windows\\GuestOS\\setup.done) '
        'else if exist C:\\ProgramData\\GuestOS\\setup.pending_reboot '
        '(echo PENDING& type C:\\ProgramData\\GuestOS\\setup.pending_reboot) '
        'else if exist C:\\Windows\\GuestOS\\setup.pending_reboot '
        '(echo PENDING& type C:\\Windows\\GuestOS\\setup.pending_reboot) '
        'else (echo MISSING)'
    )
    try:
        out = (run_command_in_guest(vmid, cmd) or '').strip()
    except Exception as e:  # noqa: BLE001
        app.logger.info(f"VM {vmid} setup marker read failed: {e}")
        return None, None
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if not lines:
        return None, None
    head = lines[0].upper()
    detail = '\n'.join(lines[1:]).strip() or None
    if head.startswith('FAILED'):
        return 'failed', detail
    if head.startswith('DONE'):
        return 'done', detail
    if head.startswith('PENDING'):
        return 'pending_reboot', detail
    return None, None


def _is_domain_join_failed_marker(setup_detail):
    """True when setup.ps1 recorded a soft domain-join failure."""
    detail = str(setup_detail or '').strip().lower()
    return detail == 'domain-join-failed' or detail.startswith('domain-join-failed')


def _join_fail_class(setup_detail):
    """Return ``not_an_ou`` / ``invalid_credentials`` / … from the marker, else ''."""
    detail = str(setup_detail or '').strip()
    if not _is_domain_join_failed_marker(detail):
        return ''
    if ':' not in detail:
        return ''
    return detail.split(':', 1)[-1].strip()


JOIN_DIAG_MAX_CHARS = 2400


def _read_join_diag(vmid):
    """Return GuestOS join-diag.txt text, or None if missing."""
    cmd = (
        'cmd.exe /c '
        'if exist C:\\ProgramData\\GuestOS\\join-diag.txt '
        '(type C:\\ProgramData\\GuestOS\\join-diag.txt) '
        'else if exist C:\\Windows\\GuestOS\\join-diag.txt '
        '(type C:\\Windows\\GuestOS\\join-diag.txt) '
        'else (echo MISSING)'
    )
    try:
        out = (run_command_in_guest(vmid, cmd) or '').strip()
    except Exception as e:  # noqa: BLE001
        app.logger.info('VM %s join-diag read failed: %s', vmid, e)
        return None
    if not out:
        return None
    first = out.splitlines()[0].strip().upper()
    if first == 'MISSING':
        return None
    if len(out) > JOIN_DIAG_MAX_CHARS:
        out = out[:JOIN_DIAG_MAX_CHARS] + '\n… (truncated)'
    return out


def _emit_join_diag(vmid, progress):
    """Copy join-diag.txt into the job event_log (one line per diag line)."""
    diag = _read_join_diag(vmid)
    if not diag:
        progress('join-diag: (missing on guest)')
        return
    for line in diag.splitlines()[:30]:
        text = line.strip()
        if text:
            progress(f'join-diag: {text}')


def _verify_sysprep_result(vmid, expected_hostname, expected_ip=None,
                           expected_domain=None, expected_ipv6=None,
                           timeout=None, on_progress=None,
                           expect_setup_reboot=False, join_meta=None):
    """Best-effort post-sysprep verification via the QEMU guest agent.

    Returns ``(summary, ok)``. ``ok`` is False when setup.ps1 never completed
    (``setup.done`` missing / ``setup.failed`` present), a required static IP
    never appears, the hostname does not match, or an expected domain join is
    not observed. When ``expected_ipv6`` is set, that address must also appear.

    ``expect_setup_reboot`` extends the wait when setup.ps1 will reboot for
    pagefile and/or domain join (``pending_reboot`` → ``done``).

    ``join_meta`` may include ``host_dc_reachable`` (bool) and planned
    ``domain_join_method`` (``odj`` / ``add-computer``) from the clone worker.
    Guest ``SetupJoinMethod`` wins when present.

    When setup marks ``domain-join-failed``, domain polling is shortened and the
    summary includes an explicit WARNING so operators see join failure quickly.
    ``join_meta`` may also include ``domain_ou``. On a soft join failure,
    ``join-diag.txt`` is pulled into the job event log.
    """
    # Always wait for FirstLogon setup.ps1 — specialize hostname alone is not enough.
    # Domain join / pagefile trigger an extra reboot after setup — allow more time.
    if timeout is None:
        if expected_domain or expect_setup_reboot:
            timeout = 1500
        elif expected_ip or expected_ipv6:
            timeout = 900
        else:
            # DHCP: still wait for setup.done (not just a short lease peek).
            timeout = 600
    poll = 15

    proxmox = get_proxmox_api()
    node = _get_vm_node(vmid)
    if not proxmox or not node:
        return "verification skipped (VM not found)", False

    def _progress(msg):
        if on_progress:
            on_progress(msg)

    polls = max(1, timeout // poll)
    setup_state = None
    setup_detail = None
    for i in range(polls):
        setup_state, setup_detail = _guest_setup_marker(vmid)
        if setup_state == 'pending_reboot':
            _progress(
                f"Guest setup pending reboot ({setup_detail or 'pagefile/domain'}) "
                f"({i + 1}/{polls})..."
            )
        elif setup_state in ('done', 'failed'):
            _progress(f"Waiting for guest setup.ps1 ({i + 1}/{polls})...")
            break
        else:
            _progress(f"Waiting for guest setup.ps1 ({i + 1}/{polls})...")
        if i + 1 < polls:
            time.sleep(poll)

    if setup_state == 'failed':
        detail = f" ({setup_detail})" if setup_detail else ''
        return f"setup.ps1 failed{detail}", False
    if setup_state == 'pending_reboot':
        return "setup.ps1 pending reboot never reached setup.done", False
    if setup_state != 'done':
        return "setup.ps1 did not complete (setup.done missing)", False

    join_failed_soft = bool(
        expected_domain and _is_domain_join_failed_marker(setup_detail)
    )
    if join_failed_soft:
        _progress(
            "WARNING: guest marked domain-join-failed — skipping long domain wait."
        )
        _emit_join_diag(vmid, _progress)

    # Hostname is set in the specialize pass; refresh after setup / possible reboot.
    actual_hostname = None
    try:
        _progress("Verifying hostname via guest agent...")
        out = run_command_in_guest(vmid, 'cmd.exe /c hostname')
        if out:
            actual_hostname = out.strip()
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"Could not read hostname for VM {vmid}: {e}")

    found_ip = None
    found_ipv6 = None
    domain_name = None
    part_of_domain = None
    # After setup.done, network/domain should settle quickly; keep a short poll.
    # Soft domain-join failure: only briefly re-check IP — do not burn 40 polls.
    if join_failed_soft:
        net_polls = 3
    else:
        net_polls = max(1, min(polls, 40 if expected_domain else 20))
    for i in range(net_polls):
        try:
            mode = f"static {expected_ip}" if expected_ip else "DHCP"
            _progress(f"Checking guest network ({mode}) ({i + 1}/{net_polls})...")
            info = proxmox.nodes(node).qemu(vmid).agent.get('network-get-interfaces')
            ips = [
                addr.get('ip-address')
                for iface in info.get('result', [])
                for addr in iface.get('ip-addresses', [])
                if addr.get('ip-address-type') == 'ipv4'
                and not str(addr.get('ip-address', '')).startswith(('127.', '169.254.'))
            ]
            ips6 = [
                addr.get('ip-address')
                for iface in info.get('result', [])
                for addr in iface.get('ip-addresses', [])
                if addr.get('ip-address-type') == 'ipv6'
                and not str(addr.get('ip-address', '')).lower().startswith(('fe80:', '::1'))
            ]
            if expected_ip:
                if expected_ip in ips:
                    found_ip = expected_ip
            elif ips:
                found_ip = ips[0]

            if expected_ipv6:
                # Compare canonical forms loosely (case / compressed).
                exp6 = str(expected_ipv6).lower()
                for candidate in ips6:
                    if str(candidate).lower() == exp6 or str(candidate).lower().startswith(exp6.split('%')[0]):
                        found_ipv6 = candidate
                        break
                    # Also accept if expanded forms match via string containment of compressed
                    if exp6 in str(candidate).lower():
                        found_ipv6 = candidate
                        break

            domain_ready = True
            if expected_domain and not join_failed_soft:
                _progress(
                    f"Checking domain membership ({expected_domain}) "
                    f"({i + 1}/{net_polls})..."
                )
                domain_name, part_of_domain = _read_domain_membership(vmid)
                domain_ready = bool(
                    part_of_domain and _domains_match(domain_name, expected_domain)
                )
            elif expected_domain and join_failed_soft and i == 0:
                # One cheap membership read for the summary (not a wait loop).
                try:
                    domain_name, part_of_domain = _read_domain_membership(vmid)
                except Exception:  # noqa: BLE001
                    domain_name, part_of_domain = None, False

            ip_ready = (found_ip is not None) if expected_ip else bool(found_ip)
            ipv6_ready = (found_ipv6 is not None) if expected_ipv6 else True
            if expected_ip or expected_domain or expected_ipv6:
                if join_failed_soft:
                    # Domain already known failed; stop once IP/IPv6 settled or polls end.
                    if (not expected_ip or ip_ready) and ipv6_ready:
                        break
                elif ip_ready and domain_ready and ipv6_ready:
                    break
            elif found_ip:
                break
        except Exception as e:  # noqa: BLE001
            app.logger.info(f"VM {vmid} agent not ready during verify: {e}")
        if i + 1 < net_polls:
            time.sleep(poll)

    # Refresh hostname after possible domain-join reboot.
    if expected_domain and not join_failed_soft:
        try:
            out = run_command_in_guest(vmid, 'cmd.exe /c hostname')
            if out:
                actual_hostname = out.strip()
        except Exception as e:  # noqa: BLE001
            app.logger.warning(f"Could not re-read hostname for VM {vmid}: {e}")

    hostname_ok = (
        actual_hostname is not None
        and expected_hostname is not None
        and actual_hostname.lower() == str(expected_hostname).lower()
    )
    parts = [
        "setup.done=ok",
        f"hostname={actual_hostname or '?'} "
        f"({'ok' if hostname_ok else 'expected ' + str(expected_hostname)})"
    ]

    ip_ok = True
    if expected_ip:
        if found_ip:
            parts.append(f"IP {expected_ip} present")
        else:
            parts.append(f"IP not assigned (expected {expected_ip} not visible)")
            ip_ok = False
    else:
        if found_ip:
            parts.append(f"DHCP IP={found_ip}")
        else:
            parts.append("IP not assigned (no DHCP lease detected)")
            # After setup.done, missing DHCP lease is a real failure.
            ip_ok = False

    ipv6_ok = True
    if expected_ipv6:
        if found_ipv6:
            parts.append(f"IPv6 {expected_ipv6} present")
        else:
            parts.append(f"IPv6 not assigned (expected {expected_ipv6} not visible)")
            ipv6_ok = False

    domain_ok = True
    join_meta = join_meta or {}
    guest_method = None
    if expected_domain:
        guest_method = _read_setup_join_method(vmid)
        planned = join_meta.get('domain_join_method')
        method = guest_method or (
            planned if planned in ('odj', 'add-computer') else None
        )
        host_dc = join_meta.get('host_dc_reachable')
        extras = []
        if method:
            extras.append(method)
        if host_dc is True:
            extras.append('host DC reachable')
        elif host_dc is False:
            extras.append('host DC unreachable')
        ou = (join_meta.get('domain_ou') or '').strip()
        extras.append('ou=' + (ou[:80] if ou else '(empty)'))
        fail_class = _join_fail_class(setup_detail)
        if fail_class:
            extras.append(f'class={fail_class}')
        suffix = (', ' + ', '.join(extras)) if extras else ''
        app.logger.info(
            'join-path: guest method=%s planned=%s host_dc=%s ou=%s class=%s',
            guest_method or '(unread)',
            planned or '(none)',
            host_dc if host_dc is not None else '(unset)',
            ou or '(empty)',
            fail_class or '(none)',
        )
        if join_failed_soft:
            marked = setup_detail or 'domain-join-failed'
            parts.append(
                f"WARNING: domain join failed (guest setup marked {marked}); "
                f"domain[{expected_domain}]: not joined "
                f"(workgroup/domain={domain_name or 'WORKGROUP'}{suffix})"
            )
            domain_ok = False
        elif part_of_domain and _domains_match(domain_name, expected_domain):
            parts.append(f"domain[{expected_domain}]: joined ({domain_name}{suffix})")
            domain_ok = True
        elif part_of_domain is False:
            parts.append(
                f"WARNING: domain join failed; domain[{expected_domain}]: not joined "
                f"(workgroup/domain={domain_name or '?'}{suffix})"
            )
            domain_ok = False
        elif domain_name and _domains_match(domain_name, expected_domain):
            # Domain string matches but PartOfDomain missing/odd — treat as ok.
            parts.append(f"domain[{expected_domain}]: joined ({domain_name}{suffix})")
            domain_ok = True
        else:
            parts.append(
                f"WARNING: domain join failed; domain[{expected_domain}]: unknown "
                f"(read Domain={domain_name or '?'}, PartOfDomain={part_of_domain}{suffix})"
            )
            domain_ok = False

    ok = hostname_ok and ip_ok and ipv6_ok and domain_ok
    return "; ".join(parts), ok


def _verify_disks(vmid, disk_guest_plan, on_progress=None):
    """Verify disk reconcile outcomes via guest agent. Returns (summary, ok)."""
    if not disk_guest_plan:
        return "disks: (none)", True

    def _progress(msg):
        if on_progress:
            on_progress(msg)

    # Compact verifier: emit ROLE|LETTER|SIZE_GB|PAGEFILE_OK|STATUS per line.
    plan_json = json.dumps(disk_guest_plan)
    ps = r'''
$ErrorActionPreference = 'Continue'
$plan = @'
__PLAN_JSON__
'@ | ConvertFrom-Json
foreach ($d in $plan) {
  $role = $d.role
  $serial = $d.serial
  $letter = $d.drive_letter
  $minGb = [int]$d.min_size_gb
  $wantPf = [bool]$d.ensure_pagefile
  $disk = Get-Disk | Where-Object { $_.SerialNumber -and ($_.SerialNumber.Trim() -eq $serial) } | Select-Object -First 1
  if (-not $disk) {
    Write-Output ("{0}|{1}|0|0|missing_serial" -f $role, $letter)
    continue
  }
  if ($disk.IsOffline -or $disk.IsReadOnly -or ($disk.OperationalStatus -ne 'Online')) {
    try {
      Set-Disk -Number $disk.Number -IsOffline $false -ErrorAction Stop
      Set-Disk -Number $disk.Number -IsReadOnly $false -ErrorAction SilentlyContinue
    } catch {}
    $disk = Get-Disk -Number $disk.Number
  }
  $part = Get-Partition -DiskNumber $disk.Number -ErrorAction SilentlyContinue |
    Where-Object { $_.DriveLetter -and $_.DriveLetter.ToString().ToUpper() -eq $letter.ToUpper() } |
    Select-Object -First 1
  if (-not $part -and $role -eq 'os') {
    $part = Get-Partition -DriveLetter C -ErrorAction SilentlyContinue | Select-Object -First 1
    $letter = 'C'
  }
  $sizeGb = 0
  if ($part) {
    $vol = Get-Volume -DriveLetter $part.DriveLetter -ErrorAction SilentlyContinue
    if ($vol) { $sizeGb = [math]::Floor($vol.Size / 1GB) }
  }
  $pfOk = 1
  if ($wantPf) {
    $pfOk = 0
    $pfs = Get-CimInstance Win32_PageFileUsage -ErrorAction SilentlyContinue
    foreach ($pf in $pfs) {
      $name = [string]$pf.Name
      if ($name -and $name.ToUpper().StartsWith(($letter.ToUpper() + ':'))) { $pfOk = 1 }
    }
    if ($pfOk -ne 1) {
      $settings = Get-CimInstance Win32_PageFileSetting -ErrorAction SilentlyContinue
      foreach ($pf in $settings) {
        $name = [string]$pf.Name
        if ($name -and $name.ToUpper().StartsWith(($letter.ToUpper() + ':'))) { $pfOk = 1 }
      }
    }
    # Registry alone is soft (takes effect after reboot). Prefer live usage.
    if ($pfOk -ne 1) {
      $pfPath = ($letter.ToUpper() + ':\pagefile.sys')
      $pfReg = (Get-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management' -Name PagingFiles -ErrorAction SilentlyContinue).PagingFiles
      $regOk = $false
      foreach ($entry in @($pfReg)) {
        if ($entry -and $entry.ToUpper().StartsWith(($letter.ToUpper() + ':'))) { $regOk = $true }
      }
      if ($regOk -and (Test-Path -LiteralPath $pfPath)) { $pfOk = 1 }
      elseif ($regOk) { $pfOk = 2 }
    }
  }
  $status = 'ok'
  # Floor(bytes/1GB) on a "64G" volume often yields 63 — allow 1G slack.
  if (-not $part) { $status = 'no_volume' }
  elseif ($sizeGb + 1 -lt $minGb) { $status = 'undersized' }
  elseif ($wantPf -and $pfOk -eq 0) { $status = 'pagefile_missing' }
  elseif ($wantPf -and $pfOk -eq 2) { $status = 'pagefile_pending_reboot' }
  Write-Output ("{0}|{1}|{2}|{3}|{4}" -f $role, $letter, $sizeGb, $pfOk, $status)
}
'''.replace('__PLAN_JSON__', plan_json)

    _progress('Verifying disks via guest agent...')
    # Write a temp verify script to avoid quoting hell through guest-exec.
    script_path = r'C:\ProgramData\GuestOS\verify-disks.ps1'
    try:
        write_file_to_guest(vmid, ps.encode('utf-8'), script_path)
        out = run_command_in_guest(
            vmid,
            f'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{script_path}"',
        ) or ''
    except Exception as e:  # noqa: BLE001
        return f'disks: verify error ({e})', False

    parts = []
    ok = True
    for line in out.splitlines():
        line = line.strip()
        if not line or '|' not in line:
            continue
        bits = line.split('|')
        if len(bits) < 5:
            continue
        role, letter, size_gb, pf_ok, status = bits[0], bits[1], bits[2], bits[3], bits[4]
        if role == 'pagefile' and status == 'ok' and pf_ok == '1':
            parts.append(f'pagefile={letter}: {size_gb}G in use')
        elif status == 'ok':
            parts.append(f'{role}={letter}: {size_gb}G ok')
        else:
            parts.append(f'{role}={letter}: {status} ({size_gb}G)')
            ok = False

    if not parts:
        return f'disks: no parseable verify output ({out[:200]!r})', False
    return 'disks: ' + '; '.join(parts), ok


