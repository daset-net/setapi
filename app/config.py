import os
from functools import lru_cache
from dataclasses import dataclass
from cryptography.fernet import Fernet


@dataclass(frozen=True)
class Settings:
    database_url: str
    redis_url: str
    encryption_key: str
    bootstrap_email: str
    bootstrap_password: str
    public_url: str
    cookie_secure: bool
    cors_origins: list[str]
    max_upload_mb: int


@lru_cache
def settings():
    key = os.environ.get('SETAPI_ENCRYPTION_KEY', '')
    try:
        Fernet(key.encode())
    except Exception as exc:
        raise RuntimeError('Configure SETAPI_ENCRYPTION_KEY with a Fernet key.') from exc
    return Settings(
        database_url=os.environ['DATABASE_URL'],
        redis_url=os.environ.get('REDIS_URL', 'redis://redis:6379/0'),
        encryption_key=key,
        bootstrap_email=os.environ.get('SETAPI_ADMIN_EMAIL', 'admin@example.com'),
        bootstrap_password=os.environ.get('SETAPI_ADMIN_PASSWORD', ''),
        public_url=os.environ.get('SETAPI_PUBLIC_URL', 'http://localhost:8055').rstrip('/'),
        cookie_secure=os.environ.get('SETAPI_COOKIE_SECURE', 'true').lower() == 'true',
        cors_origins=[x.strip() for x in os.environ.get('SETAPI_CORS_ORIGINS', '').split(',') if x.strip()],
        max_upload_mb=int(os.environ.get('SETAPI_MAX_UPLOAD_MB', '50')),
    )
