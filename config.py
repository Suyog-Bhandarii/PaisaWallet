import os
import secrets
from typing import ClassVar

from dotenv import load_dotenv

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

# Load local .env only for non-production development. Production should use the platform secret store.
if os.environ.get('ENVIRONMENT', os.environ.get('FLASK_ENV', 'development')) != 'production':
    load_dotenv(os.path.join(BASE_DIR, '.env'))


def get_database_url():
    """
    Retrieve and normalize the database URL.
    - Defaults to local SQLite instance in BASE_DIR if DATABASE_URL is not set.
    - Resolves relative SQLite paths against BASE_DIR to ensure cross-platform reliability.
    - Converts legacy 'postgres://' prefixes to 'postgresql://' for SQLAlchemy compatibility.
    """
    raw_url = os.environ.get('DATABASE_URL')
    if not raw_url:
        instance_dir = os.path.join(BASE_DIR, 'instance')
        os.makedirs(instance_dir, exist_ok=True)
        db_path = os.path.join(instance_dir, 'paisa_wallet.db').replace('\\', '/')
        return f"sqlite:///{db_path}"

    # SQLAlchemy 1.4+ / 2.0 requires postgresql:// instead of postgres://
    if raw_url.startswith('postgres://'):
        return raw_url.replace('postgres://', 'postgresql://', 1)

    # Resolve relative SQLite paths against BASE_DIR
    if raw_url.startswith('sqlite:///') and not raw_url.startswith('sqlite:///:memory:'):
        sub_path = raw_url[len('sqlite:///'):]
        if not (len(sub_path) > 1 and sub_path[1] == ':') and not sub_path.startswith('/'):
            abs_path = os.path.join(BASE_DIR, sub_path)
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            return f"sqlite:///{abs_path.replace(os.sep, '/')}"

    return raw_url


class Config:
    """Base configuration settings for Paisa Wallet."""
    ENV = os.environ.get('FLASK_ENV', 'development')
    _configured_secret_key = os.environ.get('SECRET_KEY')
    SECRET_KEY = _configured_secret_key or secrets.token_hex(32)

    # Security check: production must explicitly provide a strong secret.
    if (ENV == 'production' or os.environ.get('ENVIRONMENT') == 'production') and (
        not _configured_secret_key or len(_configured_secret_key) < 32
    ):
        raise RuntimeError(
            "CRITICAL SECURITY ERROR: SECRET_KEY must be securely configured with a strong secret in production."
        )

    # Database connection URL (supports SQLite, PostgreSQL, MySQL)
    SQLALCHEMY_DATABASE_URI = get_database_url()
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Security & Cookie Flags
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    SESSION_COOKIE_SECURE = (
        os.environ.get('SESSION_COOKIE_SECURE', 'false').lower() in ('true', '1')
        or ENV == 'production'
    )
    PERMANENT_SESSION_LIFETIME = int(os.environ.get('PERMANENT_SESSION_LIFETIME', '86400'))  # 24 hours

    # Security & Rate Limiting (Flask-Limiter)
    RATELIMIT_STORAGE_URI = os.environ.get('RATELIMIT_STORAGE_URI', 'memory://')
    RATELIMIT_STRATEGY = 'fixed-window'
    RATELIMIT_DEFAULT: ClassVar[list[str]] = []

    # Only trust forwarded client IP headers when the app is behind a known proxy count.
    TRUSTED_PROXY_HOPS = int(os.environ.get('TRUSTED_PROXY_HOPS', '0'))


class TestConfig(Config):
    """Configuration for automated test suite."""
    TESTING = True
    SECRET_KEY = 'test-secret-key-not-for-production'
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False
    RATELIMIT_ENABLED = False
    SESSION_COOKIE_SECURE = False
    TRUSTED_PROXY_HOPS = 0

