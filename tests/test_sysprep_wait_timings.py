"""Tests for gated fast_waits and sysprep wait timings."""

from app.celery_app import _sysprep_wait_timings


def test_fast_waits_ignored_without_allow(app):
    app.config['TESTING'] = False
    app.config['ALLOW_FAST_WAITS'] = False
    boot, agent = _sysprep_wait_timings({'fast_waits': True})
    assert boot >= 60
    assert agent >= 30


def test_fast_waits_honored_when_allowed(app):
    app.config['ALLOW_FAST_WAITS'] = True
    boot, agent = _sysprep_wait_timings({'fast_waits': True})
    assert boot == 30
    assert agent == 15


def test_fast_waits_honored_under_testing(app):
    # conftest sets TESTING=True
    boot, agent = _sysprep_wait_timings({'fast_waits': True})
    assert boot == 30
    assert agent == 15
