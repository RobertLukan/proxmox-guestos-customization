import os
import tempfile

# Configure the environment before importing the app so config.py loads cleanly
# without a real .env (SECRET_KEY is required, and we want an isolated test DB).
os.environ.setdefault('SECRET_KEY', 'test-secret-key')
os.environ.setdefault('WINRM_SUBNET', '192.168.100.0/24')
_db_fd, _db_path = tempfile.mkstemp(suffix='.db')
os.environ.setdefault('DATABASE_URL', f'sqlite:///{_db_path}')

import pytest

from app import app as flask_app, db
from app.models import User


@pytest.fixture()
def app():
    flask_app.config.update(TESTING=True)
    with flask_app.app_context():
        db.create_all()
        if not db.session.get(User, 1):
            user = User(id=1)
            user.set_password('changeme')
            db.session.add(user)
            db.session.commit()
    yield flask_app
    with flask_app.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()
