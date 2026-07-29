import os

from app import app

if __name__ == "__main__":
    # Never enable the interactive debugger by default (CodeQL py/flask-debug).
    debug = os.environ.get('FLASK_DEBUG', '').strip().lower() in ('1', 'true', 'yes')
    app.run(debug=debug, port=app.config['PORT'])
