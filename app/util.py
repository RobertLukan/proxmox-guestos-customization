"""Shared small helpers used across routes, remotes, disks, and tasks."""


def as_bool(value, default=False):
    """Coerce form/JSON truthy values (True, 'true', 'on', 1) to a bool."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('true', '1', 't', 'yes', 'on')
