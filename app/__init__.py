from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from celery import Celery
from flask_login import LoginManager

app = Flask(__name__)
app.config.from_object('config')
app.config['SECRET_KEY'] = app.config.get('SECRET_KEY')

# Configure SQLAlchemy
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///site.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Configure Login Manager
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# Configure Celery
app.config['CELERY_BROKER_URL'] = 'redis://localhost:6379/0'
app.config['CELERY_RESULT_BACKEND'] = 'redis://localhost:6379/0'
celery = Celery(app.name, broker=app.config['CELERY_BROKER_URL'])
celery.conf.update(app.config)

from app import routes, celery_app

from werkzeug.middleware.proxy_fix import ProxyFix

if app.config['BEHIND_REVERSE_PROXY']:
    # Apply the middleware
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
    app.config['APPLICATION_ROOT'] = '/guestos'

