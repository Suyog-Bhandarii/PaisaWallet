"""
WSGI Entry Point for Paisa Wallet.
Used by production application servers like Gunicorn, Waitress, or uWSGI.
"""
from app import create_app

app = create_app()

if __name__ == '__main__':
    app.run()
