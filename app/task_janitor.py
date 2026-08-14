"""Keep the job ledger from accumulating forever-running tasks/batches.

Two failure modes this heals:

1. **Finished batches stuck RUNNING** — child tasks already SUCCESS/FAILURE but
   the batch row was never finalized (blocks ``BULK_MAX_INFLIGHT_BATCHES``).
2. **Orphan inflight tasks** — worker died or hung; ``updated_at`` stops moving
   while status stays PENDING/STARTED/PROGRESS (blocks concurrent limits).
"""
from __future__ import annotations

from datetime import timedelta

from flask import current_app

from app import app, db
from app.models import BatchRequest, Task, _utcnow

_INFLIGHT = frozenset({'PENDING', 'STARTED', 'PROGRESS'})
_TERMINAL = frozenset({'SUCCESS', 'FAILURE', 'CANCELLED', 'REJECTED'})
_ACTIVE_BATCH = frozenset({'ACCEPTED', 'RUNNING'})


def finalize_batch_if_done(batch_id):
    """Move a batch out of ACCEPTED/RUNNING once every child task is terminal."""
    if not batch_id:
        return None
    batch = BatchRequest.query.get(batch_id)
    if not batch or batch.status not in _ACTIVE_BATCH:
        return None
    tasks = Task.query.filter_by(batch_id=batch_id).all()
    if not tasks:
        return None
    statuses = {t.status for t in tasks}
    if any(s not in _TERMINAL for s in statuses):
        return None
    if statuses <= {'SUCCESS'}:
        batch.status = 'SUCCESS'
    elif statuses <= {'CANCELLED', 'REJECTED'}:
        batch.status = 'CANCELLED'
    else:
        batch.status = 'FAILED'
    batch.updated_at = _utcnow()
    db.session.commit()
    return batch.status


def finalize_finished_batches(limit=200):
    """Finalize every active batch whose tasks are already terminal."""
    batches = (
        BatchRequest.query.filter(BatchRequest.status.in_(tuple(_ACTIVE_BATCH)))
        .order_by(BatchRequest.updated_at.asc())
        .limit(max(1, int(limit)))
        .all()
    )
    closed = []
    for batch in batches:
        new_status = finalize_batch_if_done(batch.id)
        if new_status:
            closed.append({'batch_id': batch.id, 'status': new_status})
    return closed


def _stale_after_seconds():
    try:
        return max(3600, int(current_app.config.get('TASK_STALE_AFTER_SECONDS') or 21600))
    except (TypeError, ValueError):
        return 21600


def reap_stale_inflight_tasks(stale_after_seconds=None, limit=100):
    """Fail inflight tasks with no ``updated_at`` activity for too long.

    Returns a list of ``{task_id, hostname, result_vmid}`` that were reaped.
    """
    seconds = (
        max(3600, int(stale_after_seconds))
        if stale_after_seconds is not None
        else _stale_after_seconds()
    )
    cutoff = _utcnow() - timedelta(seconds=seconds)
    stale = (
        Task.query.filter(
            Task.status.in_(tuple(_INFLIGHT)),
            Task.updated_at < cutoff,
        )
        .order_by(Task.updated_at.asc())
        .limit(max(1, int(limit)))
        .all()
    )
    if not stale:
        return []

    reaped = []
    batch_ids = set()
    clones = []
    msg = (
        f'Task marked FAILURE: no progress for {seconds // 3600}h+ '
        f'(stale/orphan worker). Check worker logs and Proxmox clone if present.'
    )
    for task in stale:
        task.status = 'FAILURE'
        task.progress = min(int(task.progress or 0), 99)
        task.error_code = 'stale'
        task.error_details = msg
        task.message = msg if len(msg) <= 512 else (msg[:509] + '...')
        task.updated_at = _utcnow()
        reaped.append({
            'task_id': task.id,
            'hostname': task.hostname,
            'result_vmid': task.result_vmid,
            'remote_id': task.remote_id,
        })
        if task.batch_id:
            batch_ids.add(task.batch_id)
        if task.result_vmid:
            clones.append((task.result_vmid, task.hostname, task.remote_id))
    db.session.commit()

    for batch_id in batch_ids:
        finalize_batch_if_done(batch_id)

    if clones:
        try:
            from app.proxmox import mark_vm_customization_failed, use_pve_override
            from app.remotes import attach_pve_override
        except Exception:  # noqa: BLE001
            app.logger.warning('Stale reaper: could not import Proxmox helpers')
        else:
            for vmid, hostname, remote_id in clones:
                try:
                    override = attach_pve_override({'remote_id': remote_id or ''})
                    with use_pve_override(override):
                        mark_vm_customization_failed(vmid, hostname=hostname)
                except Exception as e:  # noqa: BLE001
                    app.logger.warning(
                        'Stale reaper: mark failed for VM %s: %s', vmid, e
                    )

    app.logger.warning(
        'Reaped %s stale inflight task(s) (cutoff=%ss): %s',
        len(reaped),
        seconds,
        [r['task_id'][:8] for r in reaped],
    )
    return reaped


def sweep_job_ledger():
    """Heal finished batches + reap orphan inflight tasks. Safe to call often."""
    closed = finalize_finished_batches()
    reaped = reap_stale_inflight_tasks()
    return {'batches_finalized': closed, 'tasks_reaped': reaped}
