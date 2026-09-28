from urllib.parse import urlsplit, parse_qs
from uuid import uuid4
import httpx
from fastapi.testclient import TestClient
from app import db, storage, google_oauth as oauth
from app.main import app

S3 = {'bucket': 'example', 'access_key_id': 'test', 'secret_access_key': 'test'}


def panel_user(admin_client, organization_id, audience='panel'):
    credentials = {'email': uuid4().hex + '@school.test', 'password': 'Panel-password-123'}
    created = admin_client.post('/api/users', json={**credentials, 'audience': audience, 'tenant_id': organization_id})
    assert created.status_code == 201, created.text
    client = TestClient(app)
    client.headers.update({'Origin': 'http://testserver', 'X-SETAPI-CSRF': '1'})
    path = '/api/auth/login' if audience == 'panel' else '/api/app-auth/login'
    login = client.post(path, json=credentials)
    assert login.status_code == 200, login.text
    if audience == 'app':
        client.headers['Authorization'] = 'Bearer ' + login.json()['token']
    return client


def schools(admin_client):
    orgs = [admin_client.post('/api/organizations', json={'name': 'School ' + uuid4().hex}).json() for _ in range(2)]
    return orgs, [panel_user(admin_client, org['id']) for org in orgs]


def test_each_organization_sees_and_manages_only_its_storage(admin_client, monkeypatch):
    orgs, (a, b) = schools(admin_client)
    created = a.post('/api/storages', json={'name': 'Drive', 'provider': 's3', 'config': S3})
    assert created.status_code == 201, created.text
    store = created.json()
    assert store['organization_id'] == orgs[0]['id']
    # Same name in another organization is fine; twice in the same one is not.
    assert b.post('/api/storages', json={'name': 'Drive', 'provider': 's3', 'config': S3}).status_code == 201
    assert a.post('/api/storages', json={'name': 'Drive', 'provider': 's3', 'config': S3}).status_code == 409
    assert [s['id'] for s in a.get('/api/storages').json()['data']] == [store['id']]
    assert store['id'] not in str(b.get('/api/storages').json())
    assert {orgs[0]['id'], orgs[1]['id']} <= {s['organization_id'] for s in admin_client.get('/api/storages').json()['data']}
    for response in (b.post('/api/storages/' + store['id'] + '/test'),
                     b.put('/api/storages/' + store['id'], json={'name': 'x', 'provider': 's3', 'config': S3}),
                     b.post('/api/files/' + store['id'], files={'file': ('a.txt', b'leak')}),
                     b.delete('/api/storages/' + store['id'])):
        assert response.status_code == 404
    monkeypatch.setattr(storage.Storage, 'upload', lambda self, path, key, content_type='': key)
    uploaded = a.post('/api/files/' + store['id'], files={'file': ('a.txt', b'hello')})
    assert uploaded.status_code == 201, uploaded.text
    file_id = uploaded.json()['id']
    with db.connection() as conn:
        assert str(conn.execute('SELECT organization_id FROM setapi.files WHERE id=%s', (file_id,)).fetchone()['organization_id']) == orgs[0]['id']
    assert file_id in str(a.get('/api/files').json())
    assert file_id not in str(b.get('/api/files').json())
    assert b.get('/api/files/' + file_id + '/download').status_code == 404
    assert b.delete('/api/files/' + file_id).status_code == 404


def test_organization_storage_never_receives_database_backups(admin_client):
    orgs, (a, _) = schools(admin_client)
    store = a.post('/api/storages', json={'name': 'School bucket', 'provider': 's3', 'config': S3}).json()['id']
    assert admin_client.post('/api/backups', json={'storage_id': store}).status_code == 404
    assert admin_client.post('/api/backup-schedules', json={'storage_id': store}).status_code == 404
    assert a.post('/api/backups', json={'storage_id': store}).status_code == 403


def test_app_users_and_api_tokens_cannot_manage_storage(admin_client):
    orgs, _ = schools(admin_client)
    app_user = panel_user(admin_client, orgs[0]['id'], audience='app')
    assert app_user.get('/api/storages').status_code == 403
    assert app_user.post('/api/storages', json={'name': 'x', 'provider': 's3', 'config': S3}).status_code == 403
    assert app_user.post('/api/integrations/google/connect', json={}).status_code == 403


def test_organization_connects_its_own_google_drive(admin_client, monkeypatch):
    monkeypatch.setenv('SETAPI_GOOGLE_CLIENT_ID', 'env-client.apps.googleusercontent.com')
    monkeypatch.setenv('SETAPI_GOOGLE_CLIENT_SECRET', 'env-secret')
    orgs, (a, b) = schools(admin_client)
    assert a.get('/api/integrations/google/config').json() == {'configured': True}

    def post(url, **kwargs):
        body = {'id': 'school-folder'} if '/drive/' in url else {'access_token': 'x', 'refresh_token': 'school-refresh', 'scope': oauth.SCOPE}
        return httpx.Response(200, json=body, request=httpx.Request('POST', url))
    monkeypatch.setattr(oauth.httpx, 'post', post)
    begin = a.post('/api/integrations/google/connect', json={'name': 'Google Drive'})
    assert begin.status_code == 200, begin.text
    state = parse_qs(urlsplit(begin.json()['url']).query)['state'][0]
    # The authorization is bound to the session that started it.
    assert b.get(oauth.CALLBACK, params={'state': state, 'code': 'c'}, follow_redirects=False).headers['location'] == '/?google=expired#storages'
    done = a.get(oauth.CALLBACK, params={'state': state, 'code': 'c'}, follow_redirects=False)
    assert done.headers['location'] == '/?google=connected#storages'
    stores = a.get('/api/storages').json()['data']
    assert [(s['name'], s['provider'], s['organization_id']) for s in stores] == [('Google Drive', 'drive', orgs[0]['id'])]
    assert b.get('/api/storages').json()['data'] == []
    # Another school may use the same connection name, and cannot reconnect someone else's.
    assert b.post('/api/integrations/google/connect', json={'name': 'Google Drive'}).status_code == 200
    assert b.post('/api/integrations/google/connect', json={'storage_id': stores[0]['id']}).status_code == 404
