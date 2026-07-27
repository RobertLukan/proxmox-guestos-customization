"""Shared Celery/SQLite task progress helpers."""
from app import db
from app.models import Task, _utcnow


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
        db.session.commit()
