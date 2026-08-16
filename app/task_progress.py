"""Shared Celery/SQLite task progress helpers."""
from app import db
from app.models import Task, _timestamp_iso, _utcnow

# Keep the operator-visible timeline bounded (UTF-8 bytes).
EVENT_LOG_MAX_BYTES = 65536
_TRUNCATE_MARKER = '… (earlier log truncated)\n'


def _last_log_payload(event_log: str) -> str:
    if not event_log:
        return ''
    last = event_log.rstrip('\n').rsplit('\n', 1)[-1]
    parts = last.split(' ', 1)
    return parts[1] if len(parts) == 2 else last


def _trim_event_log(text: str, max_bytes: int = EVENT_LOG_MAX_BYTES) -> str:
    encoded = text.encode('utf-8')
    if len(encoded) <= max_bytes:
        return text
    marker = _TRUNCATE_MARKER.encode('utf-8')
    keep = max(0, max_bytes - len(marker))
    tail = encoded[-keep:] if keep else b''
    nl = tail.find(b'\n')
    if nl != -1:
        tail = tail[nl + 1 :]
    return (marker + tail).decode('utf-8', errors='replace')


def append_task_log(task, line: str) -> None:
    """Append one operator-visible line to ``task.event_log`` (no commit).

    Consecutive identical payloads are skipped. The column is capped so a
    noisy QGA wait loop cannot grow without bound.
    """
    if task is None:
        return
    text = ' '.join(str(line or '').split())
    if not text:
        return
    if _last_log_payload(task.event_log or '') == text:
        return
    stamped = f'{_timestamp_iso(_utcnow())} {text}'
    existing = (task.event_log or '').rstrip('\n')
    combined = f'{existing}\n{stamped}' if existing else stamped
    task.event_log = _trim_event_log(combined)


def record_host_dc_preflight(task_id, data) -> None:
    """Persist the GuestOS-host DC probe onto the job and append a log line."""
    payload = data if isinstance(data, dict) else {}
    from app.util import as_bool as _as_bool

    if not _as_bool(payload.get('join_domain'), False):
        return
    task = Task.query.get(task_id)
    if not task:
        return
    from app.task_options import options_to_json

    task.options_json = options_to_json(payload)
    reachable = payload.get('host_dc_reachable')
    target = (payload.get('host_dc_target') or '').strip()
    if reachable is True:
        suffix = f' ({target})' if target else ''
        append_task_log(task, f'AD: GuestOS host DC reachable{suffix}')
    elif reachable is False:
        append_task_log(
            task,
            'AD: GuestOS host DC unreachable from this worker '
            '(guest VLAN may still reach AD)',
        )
    db.session.commit()


def record_domain_join_method(task, method: str) -> None:
    """Append the chosen AD join path (ODJ vs late Add-Computer). No commit."""
    if method == 'odj':
        append_task_log(task, 'AD join path: Offline Domain Join (ODJ) at specialize')
    elif method == 'add-computer':
        append_task_log(task, 'AD join path: late Add-Computer after OOBE')


def update_task_progress(task_id, progress, message, result_vmid=None, result_ip_address=None):
    """Update task progress (and optional result fields)."""
    task = Task.query.get(task_id)
    if task:
        task.progress = progress
        task.message = message
        if task.status in (None, 'PENDING'):
            task.status = 'PROGRESS'
        elif task.status == 'STARTED':
            task.status = 'PROGRESS'
        task.updated_at = _utcnow()
        if result_vmid is not None:
            task.result_vmid = result_vmid
        if result_ip_address is not None:
            task.result_ip_address = result_ip_address
        append_task_log(task, message)
        db.session.commit()
