from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from app import db, reset_admin
from app.config import settings
from app.main import app


@pytest.fixture
def run(monkeypatch):
    def call(email, password, *args):
        monkeypatch.setenv('SETAPI_ADMIN_EMAIL', email)
        monkeypatch.setenv('SETAPI_ADMIN_PASSWORD', password)
        settings.cache_clear()
        reset_admin.main(list(args))
    yield call
    monkeypatch.undo()
    settings.cache_clear()


def login(email, password):
    client = TestClient(app)
    return client.post('/api/auth/login', json={'email': email, 'password': password}, headers={'Origin': 'http://testserver'})


def test_reset_creates_and_then_updates_global_admin(admin_client, run):
    email = 'recover-' + uuid4().hex[:8] + '@test.example'
    run(email, 'First-password-123')
    first = login(email, 'First-password-123')
    assert first.status_code == 200, first.text
    session = TestClient(app)
    session.cookies.update(first.cookies)
    assert session.get('/api/auth/me').json()['admin']
    run(email, 'Second-password-456')
    assert login(email, 'First-password-123').status_code == 401
    assert login(email, 'Second-password-456').status_code == 200
    # Sessions opened with the old password are revoked.
    assert session.get('/api/auth/me').status_code == 401


def test_reset_never_promotes_organization_or_app_users(admin_client, run):
    org = admin_client.post('/api/organizations', json={'name': 'School ' + uuid4().hex}).json()
    email = uuid4().hex[:8] + '@school.test'
    assert admin_client.post('/api/users', json={'email': email, 'password': 'Member-password-123', 'audience': 'panel', 'tenant_id': org['id']}).status_code == 201
    with pytest.raises(SystemExit):
        run(email, 'Takeover-password-123')
    with db.connection() as conn:
        row = conn.execute('SELECT role,tenant_id FROM setapi.users WHERE email=%s', (email,)).fetchone()
    assert row['role'] == 'member' and str(row['tenant_id']) == org['id']
    assert login(email, 'Takeover-password-123').status_code == 401


def test_reset_rejects_short_password(admin_client, run):
    with pytest.raises(SystemExit):
        run('short-' + uuid4().hex[:8] + '@test.example', 'short')


def test_reset_can_disable_mfa(admin_client, run):
    email = 'mfa-' + uuid4().hex[:8] + '@test.example'
    run(email, 'Mfa-password-12345')
    with db.connection() as conn:
        conn.execute("UPDATE setapi.users SET mfa_secret='x' WHERE email=%s", (email,))
    run(email, 'Mfa-password-12345')
    with db.connection() as conn:
        assert conn.execute('SELECT mfa_secret FROM setapi.users WHERE email=%s', (email,)).fetchone()['mfa_secret'] == 'x'
    run(email, 'Mfa-password-12345', '--disable-mfa')
    with db.connection() as conn:
        assert conn.execute('SELECT mfa_secret FROM setapi.users WHERE email=%s', (email,)).fetchone()['mfa_secret'] is None
