"""Tests for PDM launch-token HMAC helpers."""

from app.launch_token import sign_launch_token, verify_launch_token


def test_sign_and_verify_roundtrip(app):
    app.config['GUESTOS_LAUNCH_SECRET'] = 'unit-test-secret'
    app.config['GUESTOS_LAUNCH_TTL'] = 300
    with app.app_context():
        tok = sign_launch_token(120, 'vie-1')
        ok, err = verify_launch_token(
            tok['exp'], tok['template_vmid'], tok['remote_id'], tok['jti'], tok['sig'],
        )
    assert ok and err == ''


def test_verify_rejects_bad_sig(app):
    app.config['GUESTOS_LAUNCH_SECRET'] = 'unit-test-secret'
    with app.app_context():
        tok = sign_launch_token(120, 'vie-1')
        ok, err = verify_launch_token(
            tok['exp'], tok['template_vmid'], tok['remote_id'], tok['jti'], '0' * 64,
        )
    assert not ok
    assert 'signature' in err.lower()


def test_verify_rejects_reuse(app):
    app.config['GUESTOS_LAUNCH_SECRET'] = 'unit-test-secret'
    with app.app_context():
        tok = sign_launch_token(120, 'vie-1')
        assert verify_launch_token(
            tok['exp'], tok['template_vmid'], tok['remote_id'], tok['jti'], tok['sig'],
        )[0]
        ok, err = verify_launch_token(
            tok['exp'], tok['template_vmid'], tok['remote_id'], tok['jti'], tok['sig'],
        )
    assert not ok
    assert 'already used' in err.lower()


def test_verify_rejects_when_durable_jti_stores_unavailable(app, monkeypatch):
    """Fail closed: do not accept launch tokens via per-process memory alone."""
    app.config['GUESTOS_LAUNCH_SECRET'] = 'unit-test-secret'
    monkeypatch.setattr('app.launch_token._consume_jti_redis', lambda *a, **k: None)
    monkeypatch.setattr('app.launch_token._consume_jti_sqlite', lambda *a, **k: None)
    with app.app_context():
        tok = sign_launch_token(120, 'vie-1')
        ok, err = verify_launch_token(
            tok['exp'], tok['template_vmid'], tok['remote_id'], tok['jti'], tok['sig'],
        )
    assert not ok
    assert 'already used' in err.lower()


def test_verify_rejects_expired(app, monkeypatch):
    app.config['GUESTOS_LAUNCH_SECRET'] = 'unit-test-secret'
    with app.app_context():
        tok = sign_launch_token(120, 'vie-1', ttl=60)
        monkeypatch.setattr('app.launch_token.time.time', lambda: tok['exp'] + 10)
        ok, err = verify_launch_token(
            tok['exp'], tok['template_vmid'], tok['remote_id'], tok['jti'], tok['sig'],
        )
    assert not ok
    assert 'expired' in err.lower()


def test_launch_route_logs_in_and_redirects(client, app, monkeypatch):
    app.config['GUESTOS_LAUNCH_SECRET'] = 'unit-test-secret'
    monkeypatch.setattr('app.routes.require_sysprep_template', lambda vmid, **kw: 'win10')
    monkeypatch.setattr('app.routes.get_network_bridges', lambda: [{'iface': 'vmbr0'}])
    with app.app_context():
        tok = sign_launch_token(120, 'vie-1')
    resp = client.get(
        '/launch',
        query_string={
            'template_vmid': tok['template_vmid'],
            'remote_id': tok['remote_id'],
            'exp': tok['exp'],
            'jti': tok['jti'],
            'sig': tok['sig'],
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    loc = resp.headers.get('Location', '')
    assert '/sysprep_form' in loc
    assert 'template_vmid=120' in loc
