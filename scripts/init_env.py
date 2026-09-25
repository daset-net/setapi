#!/usr/bin/env python3
"""Create local secrets without dependencies. Never overwrites an existing .env."""
import base64
import os
import secrets
from pathlib import Path

target = Path(__file__).resolve().parent.parent / '.env'
postgres = secrets.token_hex(24)
redis = secrets.token_hex(24)
values = {
    'POSTGRES_PASSWORD': postgres, 'REDIS_PASSWORD': redis,
    'DATABASE_URL': f'postgresql://setapi:{postgres}@postgres:5432/setapi',
    'REDIS_URL': f'redis://:{redis}@redis:6379/0',
    'SETAPI_ENCRYPTION_KEY': base64.urlsafe_b64encode(os.urandom(32)).decode(),
    'SETAPI_ADMIN_EMAIL': 'admin@example.com', 'SETAPI_ADMIN_PASSWORD': secrets.token_urlsafe(24),
    'SETAPI_PUBLIC_URL': 'http://localhost:8055', 'SETAPI_COOKIE_SECURE': 'false',
    'SETAPI_CORS_ORIGINS': '', 'SETAPI_MAX_UPLOAD_MB': '50',
}
fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as stream:
    stream.write('\n'.join(f'{key}={value}' for key,value in values.items()) + '\n')
print(f'Created {target}. Read SETAPI_ADMIN_PASSWORD there to sign in. Do not commit this file.')
