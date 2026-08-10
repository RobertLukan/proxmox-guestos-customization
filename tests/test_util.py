"""Helpers in app.util."""
from app.util import public_error_text, sanitize_log_fragment


def test_sanitize_log_fragment_strips_newlines():
    raw = 'Guest\r\nUser name: Admin'
    cleaned = sanitize_log_fragment(raw)
    assert '\n' not in cleaned
    assert '\r' not in cleaned
    assert 'Guest' in cleaned
    assert 'Admin' in cleaned


def test_sanitize_log_fragment_truncates():
    assert sanitize_log_fragment('x' * 500, max_len=20) == 'x' * 17 + '...'


def test_public_error_text_uses_message():
    assert public_error_text(ValueError('bad vlan')) == 'bad vlan'
    assert public_error_text(RuntimeError(123), fallback='nope') == 'nope'
