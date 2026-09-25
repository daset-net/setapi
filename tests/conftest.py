import os
import secrets
import pytest
import psycopg
from cryptography.fernet import Fernet

os.environ.setdefault('DATABASE_URL', 'postgresql://batista@127.0.0.1:55432/setapi_test')
os.environ.setdefault('REDIS_URL', 'redis://127.0.0.1:56379/15')
os.environ.setdefault('SETAPI_ENCRYPTION_KEY', Fernet.generate_key().decode())
os.environ.setdefault('SETAPI_ADMIN_EMAIL', 'test@example.com')
os.environ.setdefault('SETAPI_ADMIN_PASSWORD', secrets.token_urlsafe(24))
os.environ.setdefault('SETAPI_PUBLIC_URL', 'http://testserver')
os.environ.setdefault('SETAPI_COOKIE_SECURE', 'false')


@pytest.fixture(scope='session')
def client():
    # Tests destroy ONLY a database explicitly named setapi_test.
    from psycopg.conninfo import conninfo_to_dict
    if conninfo_to_dict(os.environ['DATABASE_URL']).get('dbname') != 'setapi_test':
        raise RuntimeError('Tests require a dedicated database named setapi_test')
    with psycopg.connect(os.environ['DATABASE_URL']) as conn:
        conn.execute('DROP SCHEMA IF EXISTS data CASCADE')
        conn.execute('DROP SCHEMA IF EXISTS setapi CASCADE')
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        from app import db
        # Only our test rate limit keys, never FLUSHDB on a shared Redis.
        for key in db.cache.scan_iter('setapi:login:*'):
            db.cache.delete(key)
        yield c


@pytest.fixture
def admin_client(client):
    response = client.post('/api/auth/login', json={'email':os.environ['SETAPI_ADMIN_EMAIL'],'password':os.environ['SETAPI_ADMIN_PASSWORD']})
    assert response.status_code == 200, response.text
    client.headers.update({'Origin':'http://testserver','X-SETAPI-CSRF':'1'})
    return client
