from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from celery import Celery
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix

# Extensions are instantiated without an app here and bound to the app inside
# create_app(). Keeping them as module-level singletons preserves the existing
# ``from app import db, celery, login_manager`` import style used across the
# codebase while still following the application-factory pattern.
db = SQLAlchemy()
login_manager = LoginManager()
csrf = CSRFProtect()
celery = Celery(__name__)


def _configure_celery(app):
    """Bind Celery to the Flask app config and run each task in an app context.

    The ContextTask base removes the need for every task to wrap its body in
    ``with app.app_context()`` manually.
    """
    celery.conf.update(
        broker_url=app.config['CELERY_BROKER_URL'],
        result_backend=app.config['CELERY_RESULT_BACKEND'],
    )

    task_base = celery.Task

    class ContextTask(task_base):
        abstract = True

        def __call__(self, *args, **kwargs):
            with app.app_context():
                return task_base.__call__(self, *args, **kwargs)

    celery.Task = ContextTask
    return celery


def create_app():
    """Application factory: build and configure the Flask app + extensions."""
    app = Flask(__name__)
    app.config.from_object('config')

    # Harden session cookies. Secure is only enabled behind a reverse proxy
    # (i.e. when TLS is terminated upstream) so local HTTP development works.
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=app.config.get('BEHIND_REVERSE_PROXY', False),
    )

    # Bind extensions to the app.
    db.init_app(app)
    csrf.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = 'login'
    _configure_celery(app)

    if app.config['BEHIND_REVERSE_PROXY']:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
        app.config['APPLICATION_ROOT'] = '/guestos'

    return app


# The configured app is created once at import time so that modules importing
# ``from app import app`` (routes, tasks, run.py, wsgi.py) keep working.
app = create_app()

# Routes and Celery tasks are imported after the app exists so their
# ``@app.route`` / ``@celery.task`` / ``@login_manager.user_loader`` decorators
# register against the configured objects above.
from app import routes, celery_app  # noqa: E402,F401
