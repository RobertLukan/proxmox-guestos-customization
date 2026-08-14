"""Sysprep shutdown / reboot wait helpers."""
from __future__ import annotations

import logging
import re
import time

from app.proxmox import (
    get_proxmox_api,
    _get_vm_node,
    power_on_vm,
    run_command_in_guest,
    wait_for_guest_agent,
)
from app.task_progress import update_task_progress

# Guest Panther logs written while sysprep.exe /generalize runs.
_SETUPERR = r'C:\Windows\System32\Sysprep\Panther\setuperr.log'
_SETUPACT = r'C:\Windows\System32\Sysprep\Panther\setupact.log'

# Failures that mean Sysprep will never shut down cleanly — fail the task early.
_SYSPREP_FAIL_PATTERNS = (
    re.compile(r'SysprepGeneralizeValidate', re.I),
    re.compile(r'Error in validating the (?:provider|actions)', re.I),
    re.compile(r'Failed to remove apps for the current user', re.I),
    re.compile(r'RemoveAllApps thread', re.I),
    re.compile(r'will not function properly in the sysprep image', re.I),
    re.compile(r'Sysprep was not able to validate', re.I),
    re.compile(r'A fatal error occurred while trying to sysprep', re.I),
    re.compile(r'SYSPREP_FAILED', re.I),
    # Avoid matching Info noise like "[IE sysprep provider] RegOpenKeyEx failed".
    re.compile(r'Sysprep (?:encountered an error|failed to (?:validate|complete|generalize))', re.I),
    re.compile(r'hr=0x[0-9A-Fa-f]{8}', re.I),
)

# Panther Info/Warning lines often say "failed" for missing optional keys — not fatal.
_SYSPREP_NONFATAL_LEVEL = re.compile(r',\s*(?:Info|Warning)\s+', re.I)


class SysprepGuestFailed(Exception):
    """Raised when Panther logs show Sysprep cannot complete."""

    def __init__(self, message: str, excerpt: str = ''):
        self.excerpt = (excerpt or '').strip()
        super().__init__(message)


def _guest_agent_responsive(proxmox, node, vmid):
    """True when the QEMU Guest Agent answers a cheap probe."""
    try:
        return proxmox.nodes(node).qemu(vmid).agent.get('get-fsinfo') is not None
    except Exception:
        return False


def _read_guest_log_tail(vmid, path, lines=80):
    """Return the last ``lines`` of a guest text file, or '' if unreadable."""
    # Use cmd TYPE via PowerShell Get-Content so locked/missing files soft-fail.
    # retries=1: during Sysprep, brief lock is normal — next poll will retry.
    ps = (
        "powershell -NoProfile -Command "
        f"\"$p='{path}'; "
        "if (Test-Path -LiteralPath $p) { "
        f"Get-Content -LiteralPath $p -Tail {int(lines)} -ErrorAction SilentlyContinue "
        "| Out-String "
        "} else { '' }\""
    )
    try:
        out = run_command_in_guest(vmid, ps, retries=1, retry_delay=2) or ''
    except Exception as e:  # noqa: BLE001
        logging.info('VM %s: could not read %s (%s)', vmid, path, e)
        return ''
    return out.strip()


def _match_sysprep_failure(text: str):
    """Return (matched_line, pattern) if ``text`` looks like a hard Sysprep failure."""
    if not text:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _SYSPREP_NONFATAL_LEVEL.search(stripped):
            continue
        for pat in _SYSPREP_FAIL_PATTERNS:
            if pat.search(stripped):
                return stripped, pat.pattern
    # Also scan whole blob in case the match spans formatting oddly.
    for pat in _SYSPREP_FAIL_PATTERNS:
        m = pat.search(text)
        if m:
            # Prefer surrounding line
            for line in text.splitlines():
                if _SYSPREP_NONFATAL_LEVEL.search(line):
                    continue
                if pat.search(line):
                    return line.strip(), pat.pattern
            # Matched only inside Info/Warning noise — ignore.
            continue
    return None


def _excerpt_around_match(text: str, matched_line: str, max_chars: int = 900) -> str:
    """Build a short multi-line excerpt centered on the matched failure line."""
    lines = [ln.rstrip() for ln in (text or '').splitlines() if ln.strip()]
    if not lines:
        return (matched_line or '')[:max_chars]
    idx = 0
    for i, ln in enumerate(lines):
        if matched_line and matched_line in ln:
            idx = i
            break
    start = max(0, idx - 2)
    end = min(len(lines), idx + 4)
    chunk = '\n'.join(lines[start:end])
    if len(chunk) > max_chars:
        chunk = chunk[: max_chars - 3] + '...'
    return chunk


def probe_sysprep_panther_failure(vmid):
    """If Panther logs show a hard Sysprep failure, return a human message; else None.

    Prefer ``setuperr.log`` (often empty until finalize), then fall back to the
    tail of ``setupact.log`` (where AppX / Copilot validate errors often appear).
    """
    err_text = _read_guest_log_tail(vmid, _SETUPERR, lines=100)
    act_text = ''
    hit = _match_sysprep_failure(err_text)
    source = 'setuperr.log'
    blob = err_text
    if not hit:
        act_text = _read_guest_log_tail(vmid, _SETUPACT, lines=120)
        hit = _match_sysprep_failure(act_text)
        source = 'setupact.log'
        blob = act_text
    if not hit:
        return None
    matched_line, _pat = hit
    excerpt = _excerpt_around_match(blob, matched_line)
    # Keep task message readable; full excerpt is attached after the colon.
    summary = matched_line
    if len(summary) > 220:
        summary = summary[:217] + '...'
    return (
        f'Sysprep failed in guest ({source}): {summary}'
        + (f'\n---\n{excerpt}' if excerpt and excerpt != matched_line else '')
    )


def _wait_for_vm_stopped(vmid, timeout=900):
    """Poll until the VM reports 'stopped'. Returns True on success, else False."""
    outcome = _wait_for_sysprep_shutdown(vmid, timeout=timeout, allow_reboot=False)
    return outcome == 'stopped'


def _wait_for_sysprep_shutdown(
    vmid,
    timeout=1200,
    poll=5,
    agent_down_for=45,
    allow_reboot=True,
    on_progress=None,
    log_probe_every=20,
):
    """Wait until the guest has left the pre-sysprep running state.

    Sysprep is invoked with ``/shutdown``, so the happy path is Proxmox
    ``status=stopped``. A stop-only wait commonly hangs when:

    1. The VM powers off only briefly (or reboots into OOBE) between polls, so
       we never observe ``stopped`` even though sysprep finished.
    2. Progress stays at "waiting for shut down" while the guest is already up.

    While the guest agent is still up, periodically read Sysprep Panther logs
    and raise :class:`SysprepGuestFailed` if a hard failure is detected (e.g.
    Win11 Copilot / AppX ``SysprepGeneralizeValidate``).

    Returns:
      ``'stopped'`` — observed ``status=stopped`` (caller should power on).
      ``'running'`` — agent was down ≥ ``agent_down_for`` seconds, then came
                      back while the VM is running (caller should *not* power
                      on again). Only when ``allow_reboot`` is True.
      ``None`` — timed out.

    Raises:
      SysprepGuestFailed — Panther logs show Sysprep cannot complete.
    """
    proxmox = get_proxmox_api()
    node = _get_vm_node(vmid)
    if not proxmox or not node:
        raise Exception(f"VM {vmid} not found.")

    deadline = time.time() + timeout
    agent_down_since = None
    sustained_outage = False
    last_msg = None
    last_log_probe = 0.0
    log_interval = max(int(log_probe_every or 0), 0)

    def _progress(msg):
        nonlocal last_msg
        if msg != last_msg:
            last_msg = msg
            logging.info('VM %s [sysprep]: %s', vmid, msg)
            if on_progress:
                on_progress(msg)

    logging.info(
        'VM %s [sysprep]: waiting for shutdown/reboot (timeout=%ss, Panther probe every %ss)',
        vmid, timeout, log_interval or 'off',
    )

    while time.time() < deadline:
        status = proxmox.nodes(node).qemu(vmid).status.current.get().get('status')
        if status == 'stopped':
            _progress("VM shut down after Sysprep.")
            return 'stopped'

        agent_up = _guest_agent_responsive(proxmox, node, vmid)
        if not agent_up:
            if agent_down_since is None:
                agent_down_since = time.time()
            down_for = time.time() - agent_down_since
            if down_for >= agent_down_for:
                sustained_outage = True
            _progress(
                f"Guest agent down ({int(down_for)}s); "
                "waiting for power-off or post-sysprep boot..."
            )
        elif sustained_outage and allow_reboot and status == 'running':
            _progress(
                "Guest agent returned while VM stayed running "
                "(post-sysprep boot; stop window missed)."
            )
            return 'running'
        else:
            # Agent still answering — sysprep often runs for several minutes
            # after we issue the command (settle window returns early).
            agent_down_since = None
            now = time.time()
            if log_interval and (now - last_log_probe) >= log_interval:
                last_log_probe = now
                failure = probe_sysprep_panther_failure(vmid)
                if failure:
                    logging.warning('VM %s [sysprep]: Panther failure detected', vmid)
                    _progress(failure.split('\n', 1)[0])
                    raise SysprepGuestFailed(failure)
                logging.info(
                    'VM %s [sysprep]: Panther probe OK (no hard failure in setuperr/setupact)',
                    vmid,
                )
            _progress("Sysprep still running in guest (agent up)...")

        time.sleep(poll)

    # On timeout, try one last log probe so the caller can surface a better error.
    try:
        if _guest_agent_responsive(proxmox, node, vmid):
            failure = probe_sysprep_panther_failure(vmid)
            if failure:
                raise SysprepGuestFailed(failure)
    except SysprepGuestFailed:
        raise
    except Exception as e:  # noqa: BLE001
        logging.info('VM %s: final Panther probe failed: %s', vmid, e)

    return None


def _complete_sysprep_power_cycle(task_id, vmid, progress_base=88, agent_stable_for=60):
    """After sysprep is issued: wait for stop/reboot, power on if needed.

    Returns True on success, False if the wait timed out (caller marks FAILURE).

    Raises:
      SysprepGuestFailed — guest Panther logs show Sysprep cannot complete.
    """
    def _on_progress(msg):
        update_task_progress(task_id, progress_base, msg)

    logging.info(
        'VM %s [sysprep]: issued; waiting for shut down or reboot into OOBE '
        '(task %s)',
        vmid, task_id,
    )
    update_task_progress(
        task_id,
        progress_base,
        "Sysprep issued. Waiting for VM to shut down or reboot into OOBE...",
    )
    outcome = _wait_for_sysprep_shutdown(
        vmid,
        timeout=1200,
        on_progress=_on_progress,
    )
    if outcome is None:
        logging.warning(
            'VM %s [sysprep]: timed out waiting for shutdown/reboot', vmid,
        )
        return False

    if outcome == 'stopped':
        logging.info('VM %s [sysprep]: observed stopped; powering on', vmid)
        update_task_progress(
            task_id,
            min(progress_base + 4, 94),
            "VM shut down. Powering back on...",
        )
        power_on_vm(vmid)
    else:
        logging.info(
            'VM %s [sysprep]: still running after agent outage (missed stop window)',
            vmid,
        )
        update_task_progress(
            task_id,
            min(progress_base + 4, 94),
            "VM already running after Sysprep; waiting for guest agent...",
        )

    def _agent_progress(msg):
        # Clarify this is the post-Sysprep/OOBE wait, not guest setup.ps1 work.
        if msg.startswith('Waiting for guest agent stability'):
            msg = msg.replace(
                'Waiting for guest agent stability',
                'Waiting for guest agent after Sysprep',
                1,
            )
        elif msg.startswith('Guest agent unavailable'):
            msg = msg.replace(
                'Guest agent unavailable',
                'Guest agent unavailable after Sysprep',
                1,
            )
        update_task_progress(task_id, min(progress_base + 7, 96), msg)

    wait_for_guest_agent(
        vmid,
        timeout=1800,
        stable_for=agent_stable_for,
        on_progress=_agent_progress,
    )
    return True
