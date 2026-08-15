"""Machine-facing API authentication (Bearer / X-Api-Token).

Browser sessions continue to use Flask-Login + CSRF. Callers that present a
valid configured API token skip CSRF and do not need a login cookie — used by
PDM and other integrators for sysprep start/poll.
"""
from __future__ import annotations

from functools import wraps

from flask import g, jsonify, redirect, request, url_for
from flask_login import current_user
from flask_wtf.csrf import validate_csrf
from wtforms.validators import ValidationError as WTFValidationError

from app import app


def configured_api_tokens():
    return frozenset(app.config.get('API_TOKENS') or ())


def extract_api_token():
    auth = request.headers.get('Authorization') or ''
    if auth.lower().startswith('bearer '):
        return auth[7:].strip()
    return (request.headers.get('X-Api-Token') or '').strip()


def request_has_valid_api_token():
    token = extract_api_token()
    if not token:
        return False
    return token in configured_api_tokens()


def require_csrf_unless_api_token():
    """Validate CSRF for browser/session callers; no-op when API token auth."""
    if request.method == 'OPTIONS':
        return None
    if getattr(g, 'auth_via_api_token', False):
        return None
    token = (
        request.headers.get('X-CSRFToken')
        or request.headers.get('X-CSRF-Token')
        or (request.get_json(silent=True) or {}).get('csrf_token')
        or request.form.get('csrf_token')
    )
    try:
        validate_csrf(token)
    except WTFValidationError:
        return jsonify({'error': 'CSRF token missing or invalid.'}), 400
    return None


def login_or_api_token_required(view):
    """Allow either a logged-in browser session or a valid API token."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if request.method == 'OPTIONS':
            return view(*args, **kwargs)
        if request_has_valid_api_token():
            g.auth_via_api_token = True
            return view(*args, **kwargs)
        g.auth_via_api_token = False
        if current_user.is_authenticated:
            return view(*args, **kwargs)
        if (
            request.is_json
            or request.path.startswith('/api/')
            or request.path.startswith('/start_')
            or request.path.startswith('/task_status')
        ):
            return jsonify({'error': 'Unauthorized'}), 401
        return redirect(url_for('login', next=request.url))

    return wrapped


def with_session_csrf(view):
    """Run require_csrf_unless_api_token before the view body."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        err = require_csrf_unless_api_token()
        if err is not None:
            return err
        return view(*args, **kwargs)

    return wrapped
