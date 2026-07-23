from app import db
from datetime import datetime, timezone
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash


def _utcnow():
    """Timezone-aware UTC now (replaces the deprecated datetime.utcnow)."""
    return datetime.now(timezone.utc)

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
    status = db.Column(db.String(64), default='PENDING') # PENDING, STARTED, PROGRESS, SUCCESS, FAILURE
    progress = db.Column(db.Integer, default=0) # 0-100
    message = db.Column(db.String(512), default='')
    timestamp = db.Column(db.DateTime(timezone=True), index=True, default=_utcnow)
    result_vmid = db.Column(db.Integer, nullable=True) # New column for VMID
    result_ip_address = db.Column(db.String(64), nullable=True) # New column for IP address
    vm_uuid = db.Column(db.String(36), nullable=True) # New column for unique VM identifier
    redirect_url = db.Column(db.String(256), nullable=True) # New column for redirection URL

    def __repr__(self):
        return '<Task {}> '.format(self.name)

    def _timestamp_iso(self):
        """Return the timestamp as an ISO-8601 string with a trailing 'Z'.

        Handles both timezone-aware and legacy naive (assumed UTC) values so the
        frontend contract stays stable.
        """
        ts = self.timestamp
        if ts is None:
            return None
        if ts.tzinfo is not None:
            ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
        return ts.isoformat() + 'Z'

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'status': self.status,
            'progress': self.progress,
            'message': self.message,
            'timestamp': self._timestamp_iso(),
            'result_vmid': self.result_vmid,
            'result_ip_address': self.result_ip_address,
            'vm_uuid': self.vm_uuid, # Include in dict
            'redirect_url': self.redirect_url # Include in dict
        }

    def update_status(self, status, progress=None, message=None, result_vmid=None, result_ip_address=None, vm_uuid=None, redirect_url=None):
        self.status = status
        if progress is not None:
            self.progress = progress
        if message is not None:
            self.message = message
        if result_vmid is not None:
            self.result_vmid = result_vmid
        if result_ip_address is not None:
            self.result_ip_address = result_ip_address
        if vm_uuid is not None:
            self.vm_uuid = vm_uuid
        if redirect_url is not None:
            self.redirect_url = redirect_url
        db.session.commit()