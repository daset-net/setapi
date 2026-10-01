import os
from uuid import uuid4
import httpx
import pytest
from fastapi.testclient import TestClient
from app import db, mail, mail_providers, storage
from app.main import app

KEY = 'am_test_key_1234'
ADMIN = os.environ['SETAPI_ADMIN_EMAIL']


@pytest.fixture
def provider(monkeypatch):
    """Fake provider APIs: routes map (METHOD, url) to (status, json)."""
    calls, routes = [], {}
    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        status, body = routes.get((method, url), (404, {}))
        return httpx.Response(status, json=body, request=httpx.Request(method, url))
    monkeypatch.setattr(mail_providers.httpx, 'request', request)
    monkeypatch.delenv('SETAPI_SMTP_HOST', raising=False)
    for key in [*db.cache.scan_iter('setapi:mail:*'), *db.cache.scan_iter('setapi:login:*')]:
        db.cache.delete(key)
    with db.connection() as conn:
        conn.execute('DELETE FROM setapi.mail_queue')
    yield routes, calls
    with db.connection() as conn:
        conn.execute("DELETE FROM setapi.integrations WHERE name='mail'")
        conn.execute('DELETE FROM setapi.mail_queue')


def agentmail(routes):
    routes[('GET', 'https://api.agentmail.to/v0/inboxes')] = (200, {'count': 2, 'inboxes': [
        {'inbox_id': 'escola@agentmail.to', 'email': 'escola@agentmail.to', 'display_name': 'Escola'},
        {'inbox_id': 'suporte@agentmail.to', 'display_name': ''}]})
    routes[('POST', 'https://api.agentmail.to/v0/inboxes/escola@agentmail.to/messages/send')] = (200, {'message_id': 'm', 'thread_id': 't'})


def configure(client, routes):
    agentmail(routes)
    assert client.put('/api/mail/config', json={'provider': 'agentmail', 'api_key': KEY, 'inbox_id': 'escola@agentmail.to'}).status_code == 200


def queued():
    with db.connection() as conn:
        return [storage.decrypt(r['payload_encrypted']) for r in conn.execute('SELECT payload_encrypted FROM setapi.mail_queue ORDER BY id').fetchall()]


def school(admin_client, audience='panel'):
    org = admin_client.post('/api/organizations', json={'name': 'School ' + uuid4().hex}).json()
    email = uuid4().hex[:8] + '@school.test'
    created = admin_client.post('/api/users', json={'email': email, 'password': 'Member-password-123', 'audience': audience, 'tenant_id': org['id']})
    assert created.status_code == 201, created.text
    return org, email


def test_only_the_three_inbox_providers(admin_client, provider):
    assert [p['id'] for p in admin_client.get('/api/mail/providers').json()['data']] == ['agentmail', 'openmail', 'agmail']
    for gone in ('resend', 'brevo', 'sendgrid', 'mailgun', 'mailersend', 'smtp'):
        assert admin_client.post('/api/mail/discover', json={'provider': gone, 'api_key': 'x'}).status_code == 422


def test_agentmail_key_reveals_inboxes_and_test_goes_to_the_admin(admin_client, provider):
    routes, calls = provider
    agentmail(routes)
    found = admin_client.post('/api/mail/discover', json={'provider': 'agentmail', 'api_key': KEY})
    assert found.status_code == 200, found.text
    assert found.json()['data'] == [{'id': 'escola@agentmail.to', 'email': 'escola@agentmail.to', 'name': 'Escola'},
                                    {'id': 'suporte@agentmail.to', 'email': 'suporte@agentmail.to', 'name': ''}]
    assert calls[0][2]['headers']['Authorization'] == 'Bearer ' + KEY
    # Only an inbox the key owns can send.
    assert admin_client.put('/api/mail/config', json={'provider': 'agentmail', 'api_key': KEY, 'inbox_id': 'forjado@agentmail.to'}).status_code == 422
    assert admin_client.put('/api/mail/config', json={'provider': 'agentmail', 'api_key': KEY, 'inbox_id': 'escola@agentmail.to'}).status_code == 200
    config = admin_client.get('/api/mail/config').json()
    assert config['sender'] == 'escola@agentmail.to' and config['key_hint'] == '…1234' and ADMIN in config['recipients']
    with db.connection() as conn:
        assert KEY not in conn.execute("SELECT value_encrypted FROM setapi.integrations WHERE name='mail'").fetchone()['value_encrypted']
    # The test goes to the signed-in administrator; it cannot be pointed elsewhere.
    assert admin_client.post('/api/mail/test', json={'to': 'outro@example.com'}).status_code == 200
    assert calls[-1][2]['json']['to'] == [ADMIN]


def test_openmail_sends_by_inbox_id_once(admin_client, provider):
    routes, calls = provider
    routes[('GET', 'https://api.openmail.sh/v1/inboxes')] = (200, {'data': [{'id': 'inb_9', 'address': 'Avisos@openmail.sh', 'displayName': 'Avisos'}], 'total': 1})
    routes[('POST', 'https://api.openmail.sh/v1/inboxes/inb_9/send')] = (200, {'messageId': 'm', 'status': 'sent'})
    assert admin_client.post('/api/mail/discover', json={'provider': 'openmail', 'api_key': 'om'}).json()['data'] == [{'id': 'inb_9', 'email': 'avisos@openmail.sh', 'name': 'Avisos'}]
    assert admin_client.put('/api/mail/config', json={'provider': 'openmail', 'api_key': 'om', 'inbox_id': 'inb_9'}).json()['sender'] == 'avisos@openmail.sh'
    with db.connection() as conn:
        conn.execute("DELETE FROM setapi.mail_queue")
        mail.queue(conn, ADMIN, 'SETAPI — Aviso', 'Texto')
    mail.send_one()
    body, headers = calls[-1][2]['json'], calls[-1][2]['headers']
    assert body == {'to': ADMIN, 'subject': 'SETAPI — Aviso', 'body': 'Texto'}
    assert headers['Idempotency-Key'].startswith('setapi-mail-')


@pytest.mark.parametrize('listing', [
    [{'id': 'ag_1', 'email': 'bot@agmail.ai', 'display_name': 'Bot'}],
    {'data': [{'id': 'ag_1', 'email': 'bot@agmail.ai', 'display_name': 'Bot'}]},
    {'inboxes': [{'id': 'ag_1', 'email': 'bot@agmail.ai', 'display_name': 'Bot'}]},
])
def test_agmail_accepts_documented_listing_shapes(admin_client, provider, listing):
    routes, calls = provider
    routes[('GET', 'https://api.agmail.ai/v1/inboxes')] = (200, listing)
    routes[('POST', 'https://api.agmail.ai/v1/inboxes/ag_1/messages')] = (200, {'id': 'x', 'status': 'sent'})
    assert admin_client.put('/api/mail/config', json={'provider': 'agmail', 'api_key': 'ag_live_x', 'inbox_id': 'ag_1'}).status_code == 200
    assert admin_client.post('/api/mail/test').status_code == 200
    sent = calls[-1][2]['json']
    assert sent['to'] == ADMIN and sent['subject'] == 'SETAPI — e-mail de teste' and sent['body']


def test_rejected_key_and_unreachable_provider_show_clear_messages(admin_client, provider, monkeypatch):
    routes, _ = provider
    routes[('GET', 'https://api.openmail.sh/v1/inboxes')] = (401, {'error': 'unauthorized', 'message': 'provider-detail'})
    response = admin_client.post('/api/mail/discover', json={'provider': 'openmail', 'api_key': 'wrong'})
    assert response.status_code == 422 and 'recusou a API key' in response.text and 'provider-detail' not in response.text
    def timeout(*args, **kwargs):
        raise httpx.ConnectTimeout('slow')
    monkeypatch.setattr(mail_providers.httpx, 'request', timeout)
    assert 'Não foi possível falar com o provedor' in admin_client.post('/api/mail/discover', json={'provider': 'agmail', 'api_key': 'x'}).json()['detail']


def test_saved_key_is_reused_when_changing_inbox(admin_client, provider):
    routes, calls = provider
    configure(admin_client, routes)
    assert admin_client.put('/api/mail/config', json={'provider': 'agentmail', 'inbox_id': 'suporte@agentmail.to'}).status_code == 200
    assert calls[-1][2]['headers']['Authorization'] == 'Bearer ' + KEY
    assert admin_client.post('/api/mail/discover', json={'provider': 'openmail'}).status_code == 422


def test_password_recovery_reaches_panel_users_but_not_students(admin_client, provider):
    routes, calls = provider
    public = TestClient(app)
    assert public.post('/api/auth/forgot-password', json={'email': ADMIN}).status_code == 503
    configure(admin_client, routes)
    _, member = school(admin_client)
    _, student = school(admin_client, audience='app')
    assert public.post('/api/auth/forgot-password', json={'email': student}).status_code == 202
    assert queued() == []
    for email in (member, ADMIN):
        assert public.post('/api/auth/forgot-password', json={'email': email}).status_code == 202
        mail.send_one()
        assert calls[-1][2]['json']['to'] == [email] and 'Redefina sua senha' in calls[-1][2]['json']['subject']
    assert queued() == []


def test_provider_never_delivers_to_students_or_strangers(admin_client, provider):
    routes, calls = provider
    configure(admin_client, routes)
    _, student = school(admin_client, audience='app')
    with db.connection() as conn:
        mail.queue(conn, student, 'x', 'y')
        mail.queue(conn, 'estranho@example.com', 'x', 'y')
    before = len(calls)
    mail.send_one(); mail.send_one()
    assert len(calls) == before and queued() == []


def test_notifications_split_between_admins_and_each_school(admin_client, provider):
    routes, _ = provider
    configure(admin_client, routes)
    other = 'admin2-' + uuid4().hex[:6] + '@test.example'
    assert admin_client.post('/api/users', json={'email': other, 'password': 'Second-admin-123', 'role': 'admin'}).status_code == 201
    org_a, member_a = school(admin_client)
    _, member_b = school(admin_client)
    with db.connection() as conn:
        mail.notify_admins(conn, 'Backup falhou', 'Detalhes')
        mail.notify_organization(conn, org_a['id'], 'Google Drive conectado', 'Detalhes')
    sent = [(m['to'], m['subject']) for m in queued()]
    assert (ADMIN, 'SETAPI — Backup falhou') in sent and (other, 'SETAPI — Backup falhou') in sent
    assert (member_a, 'SETAPI — Google Drive conectado') in sent
    assert all(to != member_b for to, _ in sent)
    assert all(to not in (member_a, member_b) for to, subject in sent if subject == 'SETAPI — Backup falhou')


def test_disabled_school_stops_receiving(admin_client, provider):
    routes, _ = provider
    configure(admin_client, routes)
    org, member = school(admin_client)
    assert admin_client.patch('/api/organizations/' + org['id'], json={'active': False}).status_code == 200
    with db.connection() as conn:
        mail.notify_organization(conn, org['id'], 'Aviso', 'Texto')
        assert not mail.is_panel_address(conn, member)
    assert queued() == []


def test_registration_needs_legacy_smtp(admin_client, provider, monkeypatch):
    routes, _ = provider
    configure(admin_client, routes)
    monkeypatch.setenv('SETAPI_ALLOW_REGISTRATION', 'true')
    public = TestClient(app)
    assert public.post('/api/app-auth/register', json={'email': uuid4().hex[:8] + '@example.com', 'password': 'Student-password-123'}).status_code == 403


def test_only_global_admin_configures_mail(admin_client, provider):
    _, email = school(admin_client)
    member = TestClient(app)
    member.headers.update({'Origin': 'http://testserver', 'X-SETAPI-CSRF': '1'})
    assert member.post('/api/auth/login', json={'email': email, 'password': 'Member-password-123'}).status_code == 200
    assert member.get('/api/mail/config').status_code == 403
    assert member.post('/api/mail/discover', json={'provider': 'agentmail', 'api_key': KEY}).status_code == 403


def test_outdated_saved_config_counts_as_not_configured(admin_client, provider):
    with db.connection() as conn:
        conn.execute("INSERT INTO setapi.integrations(name,value_encrypted) VALUES('mail',%s) ON CONFLICT(name) DO UPDATE SET value_encrypted=EXCLUDED.value_encrypted",
                     (storage.encrypt({'provider': 'resend', 'api_key': 'k', 'sender': 'a@b.com'}),))
    assert admin_client.get('/api/mail/config').json()['configured'] is False
    assert not mail.ready()


def org_admin_headers(admin_client):
    org = admin_client.post('/api/organizations', json={'name': 'Org ' + uuid4().hex}).json()
    user = admin_client.post('/api/users', json={'email': uuid4().hex[:8] + '@org.test', 'password': 'Member-password-123', 'tenant_id': org['id'], 'org_admin': True})
    assert user.status_code == 201, user.text
    token = admin_client.post('/api/tokens', json={'name': 'app', 'user_id': user.json()['id']})
    assert token.status_code in (200, 201), token.text
    return org, user.json(), {'Authorization': 'Bearer ' + token.json()['token']}


def test_organization_inbox_sends_to_anyone_through_its_own_key(admin_client, provider):
    routes, calls = provider
    agentmail(routes)
    org, user, h = org_admin_headers(admin_client)
    api = TestClient(app)
    try:
        assert api.get('/api/organization-mail/config', headers=h).json() == {'configured': False}
        assert api.post('/api/organization-mail/send', headers=h, json={'to': ['a@b.com'], 'subject': 's', 'text': 't'}).status_code == 409
        assert api.put('/api/organization-mail/config', headers=h, json={'provider': 'agentmail', 'api_key': KEY, 'inbox_id': 'forjado@agentmail.to'}).status_code == 422
        r = api.put('/api/organization-mail/config', headers=h, json={'provider': 'agentmail', 'api_key': KEY, 'inbox_id': 'escola@agentmail.to'})
        assert r.status_code == 200, r.text
        assert api.get('/api/organization-mail/config', headers=h).json()['sender'] == 'escola@agentmail.to'
        # The platform inbox stays unconfigured: the organization's inbox is separate.
        assert mail.provider_config() is None
        r = api.post('/api/organization-mail/send', headers=h, json={'to': ['Cliente@Fora.com', 'cliente@fora.com'], 'subject': 'Oi', 'text': 'Texto', 'html': '<p>Oi</p>', 'reply_to': 'dono@loja.com'})
        assert r.status_code == 202 and r.json() == {'queued': 1}
        assert api.post('/api/organization-mail/send', headers=h, json={'to': ['não é e-mail'], 'subject': 's', 'text': 't'}).status_code == 422
        [job] = queued()
        assert job['to'] == 'cliente@fora.com' and job['organization_id'] == org['id']
        calls.clear()
        mail.send_one()
        assert queued() == []
        method, url, kwargs = calls[-1]
        assert url.endswith('/inboxes/escola@agentmail.to/messages/send')
        assert kwargs['json'] == {'to': ['cliente@fora.com'], 'subject': 'Oi', 'text': 'Texto', 'html': '<p>Oi</p>', 'reply_to': 'dono@loja.com'}
        assert kwargs['headers']['Authorization'] == 'Bearer ' + KEY
        # Another organization's administrator cannot see or use it; members cannot either.
        _, _, other = org_admin_headers(admin_client)
        assert api.get('/api/organization-mail/config', headers=other).json() == {'configured': False}
        assert api.get(f"/api/organization-mail/config?organization_id={org['id']}", headers=other).status_code == 404
        # The global administrator picks the organization explicitly.
        assert admin_client.get('/api/organization-mail/config').status_code == 422
        assert admin_client.get(f"/api/organization-mail/config?organization_id={org['id']}").json()['configured'] is True
        assert api.delete('/api/organization-mail/config', headers=h).status_code == 204
        assert api.get('/api/organization-mail/config', headers=h).json() == {'configured': False}
    finally:
        with db.connection() as conn:
            conn.execute("DELETE FROM setapi.integrations WHERE name LIKE 'mail:org:%'")


def test_organization_queue_is_dropped_when_its_inbox_is_removed(admin_client, provider):
    routes, calls = provider
    org, _, _ = org_admin_headers(admin_client)
    with db.connection() as conn:
        mail.queue(conn, 'x@y.com', 's', 't', organization_id=org['id'])
    calls.clear()
    mail.send_one()
    assert queued() == [] and calls == []
