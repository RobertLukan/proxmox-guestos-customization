"""SQLite-friendly schema helpers for additive Task columns."""
from __future__ import annotations

from sqlalchemy import inspect, text

from app import db


_TASK_EXTRA_COLUMNS = (
    ('updated_at', 'DATETIME'),
    ('remote_id', 'VARCHAR(128)'),
    ('template_vmid', 'INTEGER'),
    ('hostname', 'VARCHAR(128)'),
    ('batch_id', 'VARCHAR(36)'),
    ('request_id', 'VARCHAR(64)'),
    ('sequence_no', 'INTEGER'),
    ('submitter', 'VARCHAR(128)'),
    ('error_code', 'VARCHAR(64)'),
    ('error_details', 'TEXT'),
    ('options_json', 'TEXT'),
)


def ensure_task_schema():
    """Create missing tables and add new Task columns when upgrading in place."""
    db.create_all()
    bind = db.session.get_bind()
    if bind is None:
        return
    insp = inspect(bind)
    if 'task' not in insp.get_table_names():
        return
    existing = {col['name'] for col in insp.get_columns('task')}
    for name, col_type in _TASK_EXTRA_COLUMNS:
        if name in existing:
            continue
        db.session.execute(text(f'ALTER TABLE task ADD COLUMN {name} {col_type}'))
    # SQLite additive indexes for high-volume task APIs.
    db.session.execute(text('CREATE INDEX IF NOT EXISTS ix_task_batch_id ON task (batch_id)'))
    db.session.execute(text('CREATE INDEX IF NOT EXISTS ix_task_request_id ON task (request_id)'))
    db.session.execute(text('CREATE INDEX IF NOT EXISTS ix_task_status_timestamp ON task (status, timestamp)'))
    db.session.execute(text('CREATE INDEX IF NOT EXISTS ix_task_remote_timestamp ON task (remote_id, timestamp)'))
    db.session.commit()
