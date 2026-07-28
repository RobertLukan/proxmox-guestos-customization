"""Customization Spec helpers — merge named presets into deploy payloads."""
from __future__ import annotations

from app.models import CustomizationSpec
from app.windows_identity import SPEC_ALLOWED_KEYS, SPEC_SECRET_KEYS


def sanitize_spec_payload(data):
    """Return a dict safe to store (no secrets, only allowed keys)."""
    clean = {}
    for key, value in (data or {}).items():
        if key in SPEC_SECRET_KEYS:
            continue
        if key not in SPEC_ALLOWED_KEYS:
            continue
        clean[key] = value
    return clean


def resolve_spec_from_request(data):
    """If ``spec_id`` or ``spec_name`` is set, merge non-secret defaults under request.

    Request body wins over spec for any key already present (non-empty).
    Never copies administrator_password from a spec.
    Returns ``(ok, error_response_or_None)``.
    """
    if not isinstance(data, dict):
        return True, None
    spec_id = data.pop('spec_id', None)
    spec_name = (data.pop('spec_name', None) or '').strip()
    if not spec_id and not spec_name:
        return True, None

    spec = None
    if spec_id is not None and str(spec_id).strip() != '':
        try:
            spec = CustomizationSpec.query.get(int(spec_id))
        except (TypeError, ValueError):
            return False, ('Invalid spec_id.', {'spec_id': 'Must be an integer id.'})
    elif spec_name:
        spec = CustomizationSpec.query.filter_by(name=spec_name).first()

    if not spec:
        return False, ('Customization spec not found.', {'spec_id': 'Not found.', 'spec_name': 'Not found.'})

    payload = sanitize_spec_payload(spec.payload())
    for key, value in payload.items():
        if key in SPEC_SECRET_KEYS:
            continue
        current = data.get(key)
        if current in (None, '', []):
            data[key] = value
    data['applied_spec_id'] = spec.id
    data['applied_spec_name'] = spec.name
    return True, None
