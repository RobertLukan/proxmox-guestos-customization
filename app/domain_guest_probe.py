"""In-clone (QGA) domain credential probe before Sysprep."""
from __future__ import annotations

import base64
import json
import logging

from flask import current_app

from app.domain_credentials import (
    classify_ldap_error,
    format_cred_probe_failure,
    prepare_join_credentials,
)
from app.proxmox import run_command_in_guest, write_file_to_guest
from app.util import as_bool as _as_bool
from app.validators import ValidationError

_PROBE_DIR = r'C:\ProgramData\GuestOS\credprobe'
_PROBE_PS1 = _PROBE_DIR + r'\probe.ps1'
_PROBE_JSON = _PROBE_DIR + r'\cred.json'
_PROBE_OUT = _PROBE_DIR + r'\result.txt'

# Embedded guest script: wait for IP, ADSI bind, write RESULT line.
# Always exit 0 so QGA returns stdout (non-zero exits drop out-data).
_GUEST_PROBE_PS1 = r'''
$ErrorActionPreference = 'Stop'
$credPath = 'C:\ProgramData\GuestOS\credprobe\cred.json'
$outPath = 'C:\ProgramData\GuestOS\credprobe\result.txt'
function Write-Result([string]$line) {
  Set-Content -LiteralPath $outPath -Value $line -Encoding ASCII
  Write-Output $line
}
try {
  if (-not (Test-Path -LiteralPath $credPath)) {
    Write-Result 'FAIL|other|missing cred.json|guest_ip='
    exit 0
  }
  $j = Get-Content -LiteralPath $credPath -Raw -Encoding UTF8 | ConvertFrom-Json
  $guestIp = ''
  $deadline = (Get-Date).AddSeconds([int]($j.wait_seconds))
  while ((Get-Date) -lt $deadline) {
    $ips = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
      Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
      Select-Object -ExpandProperty IPAddress)
    if ($ips.Count -gt 0) { $guestIp = $ips[0]; break }
    Start-Sleep -Seconds 2
  }
  if (-not $guestIp) {
    Write-Result 'FAIL|unreachable|no non-link-local IPv4 on guest|guest_ip='
    exit 0
  }
  $targets = @()
  if ($j.dns_servers) {
    foreach ($d in ($j.dns_servers -split ',')) {
      $t = $d.Trim()
      if ($t) { $targets += $t }
    }
  }
  if ($j.domain) { $targets += [string]$j.domain }
  if ($targets.Count -eq 0) {
    Write-Result ("FAIL|unreachable|no dns_servers|guest_ip=" + $guestIp)
    exit 0
  }
  $user = [string]$j.username
  $pass = [string]$j.password
  $last = 'no LDAP target accepted bind'
  $bindTarget = ''
  foreach ($ldapHost in $targets) {
    $bindTarget = ($ldapHost + ':389')
    $ldapPath = 'LDAP://' + $ldapHost
    try {
      $entry = New-Object System.DirectoryServices.DirectoryEntry($ldapPath, $user, $pass)
      $null = $entry.NativeObject
      Write-Result ("OK|ok|OK|bind_target=" + $bindTarget + "|guest_ip=" + $guestIp)
      exit 0
    } catch {
      $last = $_.Exception.Message
      $m = $last.ToLowerInvariant()
      if ($m -match 'logon failure|username or password|invalid credentials|0x8007052e|52e') {
        Write-Result ("FAIL|invalid_credentials|" + ($last -replace '[|\r\n]',' ') + "|bind_target=" + $bindTarget + "|guest_ip=" + $guestIp)
        exit 0
      }
      if ($m -match 'account|locked|disabled|password expired|logon hours|0x8007052f|0x800703ee') {
        Write-Result ("FAIL|account_restricted|" + ($last -replace '[|\r\n]',' ') + "|bind_target=" + $bindTarget + "|guest_ip=" + $guestIp)
        exit 0
      }
    }
  }
  Write-Result ("FAIL|unreachable|" + ($last -replace '[|\r\n]',' ') + "|bind_target=" + $bindTarget + "|guest_ip=" + $guestIp)
  exit 0
} catch {
  Write-Result ("FAIL|other|" + ($_.Exception.Message -replace '[|\r\n]',' ') + "|guest_ip=")
  exit 0
}
'''


def cred_probe_enabled() -> bool:
    try:
        return bool(current_app.config.get('DOMAIN_JOIN_CRED_PROBE', True))
    except RuntimeError:
        return True


def _parse_probe_line(line: str) -> dict:
    parts = (line or '').strip().split('|')
    # OK|ok|OK|bind_target=x|guest_ip=y
    # FAIL|class|result|bind_target=x|guest_ip=y
    status = parts[0] if parts else 'FAIL'
    error_class = parts[1] if len(parts) > 1 else 'other'
    result = parts[2] if len(parts) > 2 else line
    bind_target = None
    guest_ip = None
    for p in parts[3:]:
        if p.startswith('bind_target='):
            bind_target = p.split('=', 1)[1]
        elif p.startswith('guest_ip='):
            guest_ip = p.split('=', 1)[1]
    return {
        'ok': status == 'OK',
        'class': error_class if status != 'OK' else 'ok',
        'result': result,
        'bind_target': bind_target,
        'guest_ip': guest_ip,
    }


def probe_domain_credentials_in_guest(vmid, data, *, on_progress=None) -> dict:
    """Run ADSI bind inside the clone via QGA. Raises ValidationError on failure.

    Returns success dict on OK. ``data`` must still contain domain_password
    (load secrets before calling) or domain_join_b64.
    """
    if not _as_bool(data.get('join_domain'), False):
        return {'ok': True, 'skipped': True}
    if not cred_probe_enabled():
        logging.info('Domain cred probe disabled (DOMAIN_JOIN_CRED_PROBE=false)')
        return {'ok': True, 'skipped': True}

    payload = dict(data)
    # Prefer live password; else unpack blob
    if not payload.get('domain_password') and payload.get('domain_join_b64'):
        try:
            blob = json.loads(
                base64.b64decode(payload['domain_join_b64']).decode('utf-8')
            )
            payload['domain_name'] = blob.get('domain') or payload.get('domain_name')
            payload['domain_username'] = blob.get('username') or payload.get('domain_username')
            payload['domain_password'] = blob.get('password')
            if blob.get('ou'):
                payload['domain_ou'] = blob['ou']
        except Exception as e:  # noqa: BLE001
            raise ValidationError(f'Invalid domain_join_b64 for cred probe: {e}') from e

    try:
        blob = prepare_join_credentials(payload)
    except ValidationError:
        raise

    wait_seconds = 90
    try:
        wait_seconds = max(30, int(current_app.config.get('DOMAIN_JOIN_CRED_PROBE_WAIT_SECONDS') or 90))
    except (TypeError, ValueError, RuntimeError):
        wait_seconds = 90

    if on_progress:
        on_progress('Checking domain credentials…')

    from app.domain_credentials import credential_dns_servers

    try:
        dns = ','.join(credential_dns_servers(payload))
    except ValidationError:
        dns = ''
    guest_blob = {
        'domain': blob['domain'],
        'username': blob['username'],
        'password': blob['password'],
        'dns_servers': dns,
        'wait_seconds': wait_seconds,
    }

    run_command_in_guest(
        vmid,
        r'cmd.exe /c mkdir "C:\ProgramData\GuestOS\credprobe" 2>nul',
    )
    write_file_to_guest(vmid, json.dumps(guest_blob).encode('utf-8'), _PROBE_JSON)
    write_file_to_guest(vmid, _GUEST_PROBE_PS1.encode('utf-8'), _PROBE_PS1)

    cmd = (
        r'powershell.exe -NoProfile -ExecutionPolicy Bypass -File '
        r'"C:\ProgramData\GuestOS\credprobe\probe.ps1"'
    )
    try:
        out = run_command_in_guest(vmid, cmd) or ''
    except Exception as e:  # noqa: BLE001
        cls, short = classify_ldap_error(e)
        msg = format_cred_probe_failure(
            error_class=cls if cls != 'other' else 'other',
            domain=blob['domain'],
            username=blob['username'],
            dns_servers=dns,
            result=f'QGA exec failed: {short}',
            domain_profile=(payload.get('domain_profile') or '').strip() or None,
        )
        raise ValidationError(msg) from e
    finally:
        try:
            run_command_in_guest(
                vmid,
                r'cmd.exe /c del /f /q "C:\ProgramData\GuestOS\credprobe\cred.json" '
                r'"C:\ProgramData\GuestOS\credprobe\probe.ps1" '
                r'"C:\ProgramData\GuestOS\credprobe\result.txt" 2>nul',
            )
        except Exception:  # noqa: BLE001
            logging.warning('VM %s: could not scrub cred probe artifacts', vmid)

    # Prefer last RESULT-looking line from stdout
    line = ''
    for ln in (out or '').splitlines():
        s = ln.strip()
        if s.startswith('OK|') or s.startswith('FAIL|'):
            line = s
    if not line:
        line = f'FAIL|other|empty probe output: {(out or "")[:120]}'

    parsed = _parse_probe_line(line)
    if parsed['ok']:
        logging.info(
            'VM %s domain cred probe OK bind=%s guest_ip=%s',
            int(vmid),
            parsed.get('bind_target'),
            parsed.get('guest_ip'),
        )
        return parsed

    msg = format_cred_probe_failure(
        error_class=parsed.get('class') or 'other',
        domain=blob['domain'],
        username=blob['username'],
        dns_servers=dns,
        bind_target=parsed.get('bind_target'),
        guest_ip=parsed.get('guest_ip'),
        result=parsed.get('result'),
        domain_profile=(payload.get('domain_profile') or '').strip() or None,
    )
    raise ValidationError(msg)
