"""Unit tests for the operator-visible job event log."""
from __future__ import annotations

import re

from app import db
from app.models import Task
from app.task_progress import (
    EVENT_LOG_MAX_BYTES,
    append_task_log,
    record_domain_join_method,
    record_host_dc_preflight,
    update_task_progress,
)


def _csrf_token(html):
    match = re.search(rb'name="csrf_token" value="([^"]+)"', html)
    assert match, 'csrf_token field not found'
    return match.group(1).decode()


def _login(client):
    token = _csrf_token(client.get('/login').data)
    client.post('/login', data={'password': 'changeme', 'csrf_token': token})


def test_append_task_log_stamps_dedupes_and_caps(app):
    with app.app_context():
        task = Task(id='log-1', name='Sysprep Workflow', description='x')
        db.session.add(task)
        db.session.commit()

        append_task_log(task, 'Cloning VM...')
        append_task_log(task, 'Cloning VM...')
        append_task_log(task, '  Waiting for QEMU Guest Agent  ')
        db.session.commit()

        lines = task.event_log.splitlines()
        assert len(lines) == 2
        assert lines[0].endswith('Cloning VM...')
        assert lines[1].endswith('Waiting for QEMU Guest Agent')

        # Overflow: keep the newest tail and a truncation marker.
        task.event_log = ''
        chunk = 'x' * 200
        for i in range(400):
            append_task_log(task, f'{chunk}{i}')
        assert task.event_log.startswith('… (earlier log truncated)')
        assert len(task.event_log.encode('utf-8')) <= EVENT_LOG_MAX_BYTES


def test_update_task_progress_appends_event_log(app):
    with app.app_context():
        db.session.add(Task(id='log-prog', name='Sysprep Workflow', description='x'))
        db.session.commit()
        update_task_progress('log-prog', 10, 'Cloning VM...')
        update_task_progress('log-prog', 60, 'Waiting for QEMU Guest Agent to stabilize...')
        task = db.session.get(Task, 'log-prog')
        assert task.status == 'PROGRESS'
        assert 'Cloning VM...' in task.event_log
        assert 'Waiting for QEMU Guest Agent to stabilize...' in task.event_log


def test_record_host_dc_and_join_method(app):
    with app.app_context():
        db.session.add(Task(id='log-ad', name='Sysprep Workflow', description='x'))
        db.session.commit()

        record_host_dc_preflight('log-ad', {'join_domain': False})
        task = db.session.get(Task, 'log-ad')
        assert not task.event_log

        record_host_dc_preflight('log-ad', {
            'join_domain': True,
            'domain_name': 'lab.test',
            'host_dc_reachable': True,
            'host_dc_target': '192.168.123.191:389',
        })
        task = db.session.get(Task, 'log-ad')
        assert 'GuestOS host DC reachable (192.168.123.191:389)' in task.event_log
        assert task.options()['host_dc_reachable'] is True

        record_domain_join_method(task, 'odj')
        db.session.commit()
        assert 'Offline Domain Join (ODJ) at specialize' in task.event_log

        record_host_dc_preflight('log-ad', {
            'join_domain': True,
            'host_dc_reachable': False,
        })
        task = db.session.get(Task, 'log-ad')
        assert 'GuestOS host DC unreachable' in task.event_log


def test_task_status_includes_event_log(client, app):
    app.config['API_TOKENS'] = frozenset({'good-token'})
    with app.app_context():
        task = Task(id='log-api', name='Sysprep Workflow', description='t')
        db.session.add(task)
        db.session.commit()
        append_task_log(task, 'AD join path: Offline Domain Join (ODJ) at specialize')
        db.session.commit()

    resp = client.get('/task_status/log-api', headers={'X-Api-Token': 'good-token'})
    assert resp.status_code == 200
    body = resp.get_json()
    assert 'Offline Domain Join' in body['event_log']
    assert 'join_summary' in body


def test_workflow_page_shows_job_log(client, app):
    _login(client)
    with app.app_context():
        task = Task(
            id='log-page',
            name='Sysprep Workflow',
            description='t',
            hostname='W11ODJ',
            event_log='2026-08-16T19:50:00Z AD join path: Offline Domain Join (ODJ) at specialize',
            options_json=(
                '{"join_domain":true,"domain_name":"lab.test",'
                '"domain_join_method":"odj","host_dc_reachable":true}'
            ),
        )
        db.session.add(task)
        db.session.commit()

    resp = client.get('/workflow/log-page')
    assert resp.status_code == 200
    assert b'Job log' in resp.data
    assert b'Offline Domain Join (ODJ) at specialize' in resp.data
    assert b'AD join' in resp.data
