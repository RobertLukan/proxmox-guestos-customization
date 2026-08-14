"""Unit tests for job ledger janitor (batch finalize + stale reaper)."""
from __future__ import annotations

from datetime import timedelta

from app import db
from app.models import BatchRequest, Task, _utcnow
from app.task_janitor import (
    finalize_batch_if_done,
    finalize_finished_batches,
    reap_stale_inflight_tasks,
)


def test_finalize_batch_when_all_tasks_terminal(app):
    with app.app_context():
        db.session.add(
            BatchRequest(id='b-fin', request_id='rq-fin', status='RUNNING', total_items=2, accepted_items=2)
        )
        db.session.add(Task(id='tf1', name='Sysprep Workflow', description='x', batch_id='b-fin', status='SUCCESS'))
        db.session.add(Task(id='tf2', name='Sysprep Workflow', description='x', batch_id='b-fin', status='SUCCESS'))
        db.session.commit()
        finalize_batch_if_done('b-fin')
        assert db.session.get(BatchRequest, 'b-fin').status == 'SUCCESS'

        db.session.add(
            BatchRequest(id='b-fail', request_id='rq-fail', status='RUNNING', total_items=2, accepted_items=2)
        )
        db.session.add(Task(id='ff1', name='Sysprep Workflow', description='x', batch_id='b-fail', status='SUCCESS'))
        db.session.add(Task(id='ff2', name='Sysprep Workflow', description='x', batch_id='b-fail', status='FAILURE'))
        db.session.commit()
        finalize_batch_if_done('b-fail')
        assert db.session.get(BatchRequest, 'b-fail').status == 'FAILED'


def test_reap_stale_inflight_tasks(app):
    with app.app_context():
        app.config['TASK_STALE_AFTER_SECONDS'] = 3600
        old = _utcnow() - timedelta(hours=7)
        db.session.add(
            BatchRequest(id='b-stale', request_id='rq-stale', status='RUNNING', total_items=1, accepted_items=1)
        )
        t = Task(
            id='stale-1',
            name='Sysprep Workflow',
            description='x',
            batch_id='b-stale',
            status='PROGRESS',
            progress=50,
            message='Waiting…',
        )
        t.timestamp = old
        t.updated_at = old
        db.session.add(t)
        fresh = Task(
            id='fresh-1',
            name='Sysprep Workflow',
            description='x',
            status='PROGRESS',
            progress=10,
        )
        fresh.updated_at = _utcnow()
        db.session.add(fresh)
        db.session.commit()

        reaped = reap_stale_inflight_tasks(stale_after_seconds=3600)
        assert len(reaped) == 1
        assert reaped[0]['task_id'] == 'stale-1'
        assert db.session.get(Task, 'stale-1').status == 'FAILURE'
        assert db.session.get(Task, 'stale-1').error_code == 'stale'
        assert db.session.get(Task, 'fresh-1').status == 'PROGRESS'
        finalize_finished_batches()
        assert db.session.get(BatchRequest, 'b-stale').status == 'FAILED'
