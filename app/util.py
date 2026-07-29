"""Shared small helpers used across routes, remotes, disks, and tasks."""


def as_bool(value, default=False):
    """Coerce form/JSON truthy values (True, 'true', 'on', 1) to a bool."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('true', '1', 't', 'yes', 'on')


def sanitize_log_fragment(value, max_len=200):
    """Collapse control chars so log lines cannot be split/injected."""
    text = ' '.join(str(value).replace('\x00', '').split())
    if len(text) > max_len:
        return text[: max_len - 3] + '...'
    return text


def public_error_text(exc, fallback='Request failed.'):
    """User-facing error text from a controlled exception (never a traceback).

    Uses ``exc.args[0]`` when it is a short string we raised ourselves
    (``ValueError`` / ``ValidationError``). Unexpected types get ``fallback``.
    """
    parts = getattr(exc, 'args', ()) or ()
    if not parts or not isinstance(parts[0], str):
        return fallback
    cleaned = ' '.join(parts[0].replace('\x00', '').split())
    if not cleaned:
        return fallback
    return cleaned[:500]
