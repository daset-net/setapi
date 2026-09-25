"""Dependency-free configuration generation shared by both installers."""
import base64
import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit

SERVICES = ('setapi_app', 'setapi_db', 'setapi_redis')


def origin(value):
    value = value.rstrip('/')
    parsed = urlsplit(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise ValueError('A URL deve conter somente a origem: https://api.exemplo.com')
    if any(c.isspace() for c in value) or any(c in value for c in "'\"`\\$"):
        raise ValueError('URL inválida')
    return value


def new_values(email='admin@setapi.local', public_url='http://localhost:8055', bind_host='127.0.0.1', port=8055):
    public_url = origin(public_url)
    if not re.fullmatch(r'[A-Za-z0-9.!_+%-]+@[A-Za-z0-9.-]+', email):
        raise ValueError('E-mail administrativo inválido')
    if bind_host not in ('127.0.0.1', '0.0.0.0') or not 1 <= port <= 65535:
        raise ValueError('Use bind 127.0.0.1 ou 0.0.0.0 e uma porta válida')
    pg_password, redis_password = secrets.token_hex(24), secrets.token_hex(24)
    return {
        'POSTGRES_PASSWORD': pg_password,
        'REDIS_PASSWORD': redis_password,
        'DATABASE_URL': f'postgresql://setapi:{pg_password}@setapi_db:5432/setapi',
        'REDIS_URL': f'redis://default:{redis_password}@setapi_redis:6379/0',
        'SETAPI_ENCRYPTION_KEY': base64.urlsafe_b64encode(os.urandom(32)).decode(),
        'SETAPI_ADMIN_EMAIL': email,
        'SETAPI_ADMIN_PASSWORD': secrets.token_urlsafe(24),
        'SETAPI_PUBLIC_URL': public_url,
        'SETAPI_COOKIE_SECURE': str(public_url.startswith('https://')).lower(),
        'SETAPI_RUN_WORKER': 'true',
        'SETAPI_CORS_ORIGINS': '',
        'SETAPI_MAX_UPLOAD_MB': '50',
        'SETAPI_BIND_HOST': bind_host,
        'SETAPI_PORT': str(port),
    }


def encode_env(values):
    for key, value in values.items():
        if not re.fullmatch(r'[A-Z_][A-Z_0-9]*', key) or '\n' in value or '\r' in value:
            raise ValueError('Configuração ENV inválida')
    return '\n'.join(f'{key}={value}' for key, value in values.items()) + '\n'


def load_env(path):
    values = {}
    for line in Path(path).read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, separator, value = line.partition('=')
        if not separator or not re.fullmatch(r'[A-Z_][A-Z_0-9]*', key):
            raise ValueError('Arquivo .env inválido; revise o arquivo sem executá-lo como shell')
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    for key in ('POSTGRES_PASSWORD', 'REDIS_PASSWORD', 'DATABASE_URL', 'REDIS_URL',
                'SETAPI_ENCRYPTION_KEY', 'SETAPI_ADMIN_EMAIL', 'SETAPI_ADMIN_PASSWORD', 'SETAPI_PUBLIC_URL'):
        if not values.get(key) or values[key].startswith('replace_'):
            raise ValueError(f'Configure {key} no .env antes de instalar')
    if len(base64.urlsafe_b64decode(values['SETAPI_ENCRYPTION_KEY'])) != 32:
        raise ValueError('Chave de criptografia inválida')
    origin(values['SETAPI_PUBLIC_URL'])
    return values


def write_private(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(content)
