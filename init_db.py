from app import app, db
from app.models import User
from app.schema import ensure_task_schema

with app.app_context():
    ensure_task_schema()
    # Create a default user
    if not User.query.get(1):
        user = User(id=1)
        user.set_password('changeme')
        db.session.add(user)
        db.session.commit()
        print("Default user created with password 'changeme'.")
    print("Database initialized.")
