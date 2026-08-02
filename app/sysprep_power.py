"""Sysprep shutdown / reboot wait helpers."""
from __future__ import annotations

import time

from app.proxmox import get_proxmox_api, _get_vm_node, power_on_vm, wait_for_guest_agent
from app.task_progress import update_task_progress

def _guest_agent_responsive(proxmox, node, vmid):
    """True when the QEMU Guest Agent answers a cheap probe."""
    try:
        return proxmox.nodes(node).qemu(vmid).agent.get('get-fsinfo') is not None
    except Exception:
        return False


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
):
    """Wait until the guest has left the pre-sysprep running state.

    Sysprep is invoked with ``/shutdown``, so the happy path is Proxmox
    ``status=stopped``. A stop-only wait commonly hangs when:

    1. The VM powers off only briefly (or reboots into OOBE) between polls, so
       we never observe ``stopped`` even though sysprep finished.
    2. Progress stays at "waiting for shut down" while the guest is already up.

    Returns:
      ``'stopped'`` — observed ``status=stopped`` (caller should power on).
      ``'running'`` — agent was down ≥ ``agent_down_for`` seconds, then came
                      back while the VM is running (caller should *not* power
                      on again). Only when ``allow_reboot`` is True.
      ``None`` — timed out.
    """
    proxmox = get_proxmox_api()
    node = _get_vm_node(vmid)
    if not proxmox or not node:
        raise Exception(f"VM {vmid} not found.")

    deadline = time.time() + timeout
    agent_down_since = None
    sustained_outage = False
    last_msg = None

    def _progress(msg):
        nonlocal last_msg
        if on_progress and msg != last_msg:
            last_msg = msg
            on_progress(msg)

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
            _progress("Sysprep still running in guest (agent up)...")

        time.sleep(poll)

    return None


def _complete_sysprep_power_cycle(task_id, vmid, progress_base=88, agent_stable_for=60):
    """After sysprep is issued: wait for stop/reboot, power on if needed.

    Returns True on success, False if the wait timed out (caller marks FAILURE).
    """
    def _on_progress(msg):
        update_task_progress(task_id, progress_base, msg)

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
        return False

    if outcome == 'stopped':
        update_task_progress(
            task_id,
            min(progress_base + 4, 94),
            "VM shut down. Powering back on...",
        )
        power_on_vm(vmid)
    else:
        update_task_progress(
            task_id,
            min(progress_base + 4, 94),
            "VM already running after Sysprep; waiting for guest agent...",
        )

    def _agent_progress(msg):
        update_task_progress(task_id, min(progress_base + 7, 96), msg)

    wait_for_guest_agent(
        vmid,
        timeout=1800,
        stable_for=agent_stable_for,
        on_progress=_agent_progress,
    )
    return True

