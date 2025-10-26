from app import app, db

if __name__ == '__main__':
    with app.app_context():
        db.create_all() # Create database tables
    app.run(debug=True, port=5001)
