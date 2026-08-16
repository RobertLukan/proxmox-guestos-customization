from app import db
from datetime import datetime, timezone
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash


def _utcnow():
    """Timezone-aware UTC now (replaces the deprecated datetime.utcnow)."""
    return datetime.now(timezone.utc)


def _timestamp_iso(ts):
    """Return a timestamp as an ISO-8601 string with a trailing 'Z'."""
    if ts is None:
        return None
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts.isoformat() + 'Z'

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    password_hash = db.Column(db.String(128))

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class Task(db.Model):
    id = db.Column(db.String(36), primary_key=True) # Celery task ID
    name = db.Column(db.String(128), index=True)
    description = db.Column(db.String(256))
    status = db.Column(db.String(64), default='PENDING', index=True) # PENDING, STARTED, PROGRESS, SUCCESS, FAILURE
    progress = db.Column(db.Integer, default=0) # 0-100
    message = db.Column(db.String(512), default='')
    timestamp = db.Column(db.DateTime(timezone=True), index=True, default=_utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), index=True, default=_utcnow, onupdate=_utcnow)
    result_vmid = db.Column(db.Integer, nullable=True) # New column for VMID
    result_ip_address = db.Column(db.String(64), nullable=True) # New column for IP address
    vm_uuid = db.Column(db.String(36), nullable=True) # New column for unique VM identifier
    redirect_url = db.Column(db.String(256), nullable=True) # New column for redirection URL
    # Customization ledger fields (PDM GuestOS jobs tab / history).
    remote_id = db.Column(db.String(128), nullable=True, index=True)
    template_vmid = db.Column(db.Integer, nullable=True, index=True)
    hostname = db.Column(db.String(128), nullable=True, index=True)
    batch_id = db.Column(db.String(36), nullable=True, index=True)
    request_id = db.Column(db.String(64), nullable=True, index=True)
    sequence_no = db.Column(db.Integer, nullable=True)
    submitter = db.Column(db.String(128), nullable=True)
    error_code = db.Column(db.String(64), nullable=True)
    error_details = db.Column(db.Text, nullable=True)
    # Sanitized customization snapshot (no passwords) for Jobs history.
    options_json = db.Column(db.Text, nullable=True)
    # Append-only operator timeline (progress, AD join path, DC reachability).
    event_log = db.Column(db.Text, nullable=True)

    def __repr__(self):
        return '<Task {}> '.format(self.name)

    def _timestamp_iso(self, ts):
        return _timestamp_iso(ts)

    def options(self):
        from app.task_options import parse_options_json

        return parse_options_json(self.options_json)

    def options_chips(self):
        from app.task_options import options_summary_chips

        return options_summary_chips(self.options())

    def join_summary_lines(self):
        from app.task_options import join_summary_lines

        return join_summary_lines(self.options())

    def to_dict(self, include_log=False):
        payload = {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'status': self.status,
            'progress': self.progress,
            'message': self.message,
            'timestamp': self._timestamp_iso(self.timestamp),
            'updated_at': self._timestamp_iso(self.updated_at or self.timestamp),
            'result_vmid': self.result_vmid,
            'result_ip_address': self.result_ip_address,
            'vm_uuid': self.vm_uuid,
            'redirect_url': self.redirect_url,
            'remote_id': self.remote_id,
            'template_vmid': self.template_vmid,
            'hostname': self.hostname,
            'batch_id': self.batch_id,
            'request_id': self.request_id,
            'sequence_no': self.sequence_no,
            'submitter': self.submitter,
            'error_code': self.error_code,
            'error_details': self.error_details,
            'options': self.options(),
            'options_chips': self.options_chips(),
            'join_summary': self.join_summary_lines(),
        }
        if include_log:
            payload['event_log'] = self.event_log or ''
        return payload

    def update_status(self, status, progress=None, message=None, result_vmid=None, result_ip_address=None, vm_uuid=None, redirect_url=None, error_code=None, error_details=None):
        self.status = status
        self.updated_at = _utcnow()
        if progress is not None:
            self.progress = progress
        if message is not None:
            self.message = message
            from app.task_progress import append_task_log

            append_task_log(self, message)
        if result_vmid is not None:
            self.result_vmid = result_vmid
        if result_ip_address is not None:
            self.result_ip_address = result_ip_address
        if vm_uuid is not None:
            self.vm_uuid = vm_uuid
        if redirect_url is not None:
            self.redirect_url = redirect_url
        if error_code is not None:
            self.error_code = error_code
        if error_details is not None:
            self.error_details = error_details
        db.session.commit()


class CustomizationSpec(db.Model):
    """Named reusable customization presets (non-secret fields only)."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False, unique=True, index=True)
    description = db.Column(db.String(512), default='')
    # JSON object of wizard/API defaults — never store administrator_password.
    payload_json = db.Column(db.Text, nullable=False, default='{}')
    timestamp = db.Column(db.DateTime(timezone=True), index=True, default=_utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), index=True, default=_utcnow, onupdate=_utcnow)

    def payload(self):
        import json
        try:
            data = json.loads(self.payload_json or '{}')
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def set_payload(self, data):
        import json
        from app.windows_identity import SPEC_ALLOWED_KEYS, SPEC_SECRET_KEYS
        clean = {}
        for key, value in (data or {}).items():
            if key in SPEC_SECRET_KEYS:
                continue
            if key not in SPEC_ALLOWED_KEYS:
                continue
            clean[key] = value
        self.payload_json = json.dumps(clean, separators=(',', ':'), sort_keys=True)

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description or '',
            'payload': self.payload(),
            'timestamp': _timestamp_iso(self.timestamp),
            'updated_at': _timestamp_iso(self.updated_at or self.timestamp),
        }


class BatchRequest(db.Model):
    """Batch submission ledger for idempotency and operator status views."""
    id = db.Column(db.String(36), primary_key=True)
    request_id = db.Column(db.String(64), nullable=False, unique=True, index=True)
    status = db.Column(db.String(32), nullable=False, default='ACCEPTED', index=True)
    submitted_by = db.Column(db.String(128), nullable=True)
    remote_id = db.Column(db.String(128), nullable=True, index=True)
    template_vmid = db.Column(db.Integer, nullable=True, index=True)
    total_items = db.Column(db.Integer, nullable=False, default=0)
    accepted_items = db.Column(db.Integer, nullable=False, default=0)
    rejected_items = db.Column(db.Integer, nullable=False, default=0)
    cancelled_items = db.Column(db.Integer, nullable=False, default=0)
    idempotent_replay = db.Column(db.Boolean, nullable=False, default=False)
    message = db.Column(db.String(512), default='')
    timestamp = db.Column(db.DateTime(timezone=True), index=True, default=_utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), index=True, default=_utcnow, onupdate=_utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'request_id': self.request_id,
            'status': self.status,
            'submitted_by': self.submitted_by,
            'remote_id': self.remote_id,
            'template_vmid': self.template_vmid,
            'total_items': self.total_items,
            'accepted_items': self.accepted_items,
            'rejected_items': self.rejected_items,
            'cancelled_items': self.cancelled_items,
            'idempotent_replay': bool(self.idempotent_replay),
            'message': self.message,
            'timestamp': _timestamp_iso(self.timestamp),
            'updated_at': _timestamp_iso(self.updated_at or self.timestamp),
        }
