from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from app import db
from app.main import app
from app.worker import publish_events

ALL = ['read', 'create', 'update', 'delete']


def org(c):
    return c.post('/api/organizations', json={'name': 'Org ' + uuid4().hex}).json()['id']


def member(c, organization, scopes):
    user = c.post('/api/users', json={'email': uuid4().hex[:8] + '@org.test', 'password': 'Member-password-123', 'tenant_id': organization, 'scopes': scopes})
    assert user.status_code == 201, user.text
    token = c.post('/api/tokens', json={'name': 'member', 'user_id': user.json()['id'], 'scopes': scopes})
    assert token.status_code == 201, token.text
    return {'Authorization': 'Bearer ' + token.json()['token']}


def create(c, name, organization=None, columns=None):
    params = {'organization_id': organization} if organization else {}
    response = c.post('/api/tables', params=params, json={'name': name, 'columns': columns or [{'name': 'nome', 'type': 'text'}]})
    assert response.status_code == 201, response.text
    return response.json()


def names(c, organization=None, headers=None):
    params = {'organization_id': organization} if organization else {}
    return [t['name'] for t in c.get('/api/tables', params=params, headers=headers or {}).json()['data']]


def test_each_organization_has_its_own_tables_with_the_same_name(admin_client):
    c = admin_client
    a, b = org(c), org(c)
    name = 'clientes_' + uuid4().hex[:6]
    assert create(c, name, a)['organization_id'] == a
    create(c, name, b, [{'name': 'razao_social', 'type': 'text'}, {'name': 'cnpj', 'type': 'text'}])
    assert name in names(c, a) and name in names(c, b) and name not in names(c)
    columns = {t['name']: [col['name'] for col in t['columns']] for t in c.get('/api/tables', params={'organization_id': b}).json()['data']}
    assert 'cnpj' in columns[name] and 'nome' not in columns[name]
    # Same name at platform level is a third, independent table.
    create(c, name)
    assert name in names(c)
    ra = c.post('/api/data/' + name, params={'organization_id': a}, json={'nome': 'A'}).json()['data']['id']
    assert c.get('/api/data/' + name, params={'organization_id': b}).json()['total'] == 0
    assert c.get(f'/api/data/{name}/{ra}', params={'organization_id': b}).status_code == 404
    assert c.get(f'/api/data/{name}/{ra}', params={'organization_id': a}).json()['data']['nome'] == 'A'
    assert c.get('/api/data/' + name).json()['total'] == 0


def test_organization_users_work_only_in_their_own_organization(admin_client):
    c = admin_client
    a, b = org(c), org(c)
    name = 'pedidos_' + uuid4().hex[:6]
    create(c, name, a)
    create(c, name, b)
    ha, hb = member(c, a, {name: ALL}), member(c, b, {name: ALL})
    # No organization_id needed: members always work in their own organization.
    mine = c.post('/api/data/' + name, headers=ha, json={'nome': 'da A'})
    assert mine.status_code == 201, mine.text
    record = mine.json()['data']['id']
    assert c.get('/api/data/' + name, headers=hb).json()['total'] == 0
    assert c.get(f'/api/data/{name}/{record}', headers=hb).status_code == 404
    assert c.patch(f'/api/data/{name}/{record}', headers=hb, json={'nome': 'x'}).status_code == 404
    assert c.delete(f'/api/data/{name}/{record}', headers=hb).status_code == 404
    # Pointing at another organization is refused.
    assert c.get('/api/data/' + name, headers=ha, params={'organization_id': b}).status_code == 404
    assert c.get('/api/tables', headers=ha, params={'organization_id': b}).status_code == 404
    assert names(c, headers=ha) == [name]
    # Structure stays with the administrator.
    assert c.post('/api/tables', headers=ha, json={'name': 'nova'}).status_code == 403
    assert c.post(f'/api/tables/{name}/columns', headers=ha, json={'name': 'x'}).status_code == 403


def test_field_changes_follow_the_organization_policy(admin_client):
    c = admin_client
    a = org(c)
    name = 'produtos_' + uuid4().hex[:6]
    create(c, name, a, [{'name': 'nome', 'type': 'text'}, {'name': 'preco', 'type': 'decimal'}])
    h = member(c, a, {name: ALL})
    p = {'organization_id': a}
    assert c.post(f'/api/tables/{name}/columns', params=p, json={'name': 'estoque', 'type': 'integer'}).status_code == 201
    assert c.patch(f'/api/tables/{name}/columns/preco', params=p, json={'name': 'valor'}).status_code == 200
    assert c.put(f'/api/tables/{name}/columns/valor', params=p, json={'type': 'integer', 'confirm': f'{name}.valor'}).status_code == 200
    assert c.delete(f'/api/tables/{name}/columns/nome', params={**p, 'confirm': f'{name}.nome'}).status_code == 204
    created = c.post('/api/data/' + name, headers=h, json={'valor': 10, 'estoque': 3})
    assert created.status_code == 201, created.text
    assert set(created.json()['data']) >= {'valor', 'estoque'} and 'nome' not in created.json()['data']
    policy = c.get(f'/api/tables/{name}/policy', params=p).json()
    assert policy['table_name'] == name and 'valor' in policy['write_fields'] and 'preco' not in policy['read_fields']


def test_relations_stay_inside_the_organization(admin_client):
    c = admin_client
    a, b = org(c), org(c)
    parent = 'categorias_' + uuid4().hex[:6]
    create(c, parent, a)
    child = c.post('/api/tables', params={'organization_id': a}, json={'name': 'itens_' + uuid4().hex[:6], 'columns': [{'name': 'categoria', 'type': 'uuid', 'references': parent}]})
    assert child.status_code == 201, child.text
    assert c.post('/api/tables', params={'organization_id': b}, json={'name': 'itens_' + uuid4().hex[:6], 'columns': [{'name': 'categoria', 'type': 'uuid', 'references': parent}]}).status_code == 422


def test_dropping_a_table_keeps_other_organizations_access(admin_client):
    c = admin_client
    a, b = org(c), org(c)
    name = 'agenda_' + uuid4().hex[:6]
    create(c, name, a)
    create(c, name, b)
    hb = member(c, b, {name: ALL})
    assert c.delete('/api/tables/' + name, params={'organization_id': a, 'confirm': name}).status_code == 204
    assert name not in names(c, a) and name in names(c, b)
    assert c.post('/api/data/' + name, headers=hb, json={'nome': 'ainda funciona'}).status_code == 201


def test_reserved_names_and_unknown_organization(admin_client):
    c = admin_client
    assert c.post('/api/tables', json={'name': 'o0123456789_x'}).status_code == 422
    assert c.get('/api/tables', params={'organization_id': str(uuid4())}).status_code == 404
    assert c.post('/api/tables', params={'organization_id': str(uuid4())}, json={'name': 'x_' + uuid4().hex[:6]}).status_code == 404
    a = org(c)
    assert c.post('/api/tables', params={'organization_id': a}, json={'name': 'y_' + uuid4().hex[:6], 'organization_isolated': True}).status_code == 422


def test_realtime_uses_the_table_name_seen_by_the_organization(admin_client):
    c = admin_client
    a, b = org(c), org(c)
    name = 'chamados_' + uuid4().hex[:6]
    create(c, name, a)
    create(c, name, b)
    ha, hb = member(c, a, {name: ALL}), member(c, b, {name: ALL})
    while publish_events():
        pass
    with c.websocket_connect('/ws', headers={'origin': 'http://testserver'}) as ws:
        ws.send_json({'token': ha['Authorization'][7:], 'tables': [name]})
        assert ws.receive_json()['type'] == 'ready'
        c.post('/api/data/' + name, headers=hb, json={'nome': 'outra organização'})
        own = c.post('/api/data/' + name, headers=ha, json={'nome': 'minha'}).json()['data']
        while publish_events():
            pass
        event = ws.receive_json()
        assert event['id'] == own['id'] and event['table'] == name


def test_activity_and_storage_follow_the_organization(admin_client):
    c = admin_client
    a = org(c)
    name = 'log_' + uuid4().hex[:6]
    create(c, name, a)
    activity = c.get('/api/audit', params={'organization_id': a}).json()['data']
    assert any(e['action'] == 'table.create' and e['resource'] == name for e in activity)
    store = c.post('/api/storages', json={'name': 'S3 ' + uuid4().hex[:6], 'provider': 's3', 'organization_id': a,
                                          'config': {'bucket': 'b', 'access_key_id': 'a', 'secret_access_key': 's'}})
    assert store.status_code == 201 and store.json()['organization_id'] == a
    h = member(c, a, {})
    assert c.post('/api/storages', headers=h, json={'name': 'x', 'provider': 's3', 'organization_id': org(c),
                                                    'config': {'bucket': 'b', 'access_key_id': 'a', 'secret_access_key': 's'}}).status_code == 403


def test_mcp_tools_expose_the_organization(admin_client):
    token = admin_client.post('/api/tokens', json={'name': 'mcp', 'admin': True}).json()['token']
    client = TestClient(app)
    tools = client.post('/mcp', headers={'Authorization': 'Bearer ' + token}, json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}).json()['result']['tools']
    schemas = {t['name']: t['inputSchema']['properties'] for t in tools}
    for tool in ('list_tables', 'create_table', 'add_column', 'drop_table', 'list_records', 'create_record', 'get_policy'):
        assert 'organization_id' in schemas[tool], tool


def org_admin(c, organization):
    credentials = {'email': 'admin-' + uuid4().hex[:8] + '@org.test', 'password': 'OrgAdmin-password-123'}
    user = c.post('/api/users', json={**credentials, 'tenant_id': organization, 'org_admin': True})
    assert user.status_code == 201, user.text
    token = c.post('/api/tokens', json={'name': 'org admin', 'user_id': user.json()['id'], 'admin': True})
    assert token.status_code == 201, token.text
    return {'Authorization': 'Bearer ' + token.json()['token']}, credentials, user.json()['id']


def test_organization_administrator_builds_only_inside_its_organization(admin_client):
    c = admin_client
    a, b = org(c), org(c)
    h, _, _ = org_admin(c, a)
    name = 'estoque_' + uuid4().hex[:6]
    created = c.post('/api/tables', headers=h, json={'name': name, 'columns': [{'name': 'item', 'type': 'text'}]})
    assert created.status_code == 201, created.text
    assert created.json()['organization_id'] == a
    assert name in names(c, a) and name not in names(c) and name not in names(c, b)
    assert c.post(f'/api/tables/{name}/columns', headers=h, json={'name': 'qtd', 'type': 'integer'}).status_code == 201
    assert c.patch(f'/api/tables/{name}/columns/qtd', headers=h, json={'name': 'quantidade'}).status_code == 200
    assert c.post(f'/api/tables/{name}/indexes', headers=h, json={'columns': ['item']}).status_code == 201
    assert c.put(f'/api/tables/{name}/policy', headers=h, json={'read_fields': ['id', 'item'], 'write_fields': ['item']}).status_code == 200
    # Every record and field of its own tables, without scopes.
    record = c.post('/api/data/' + name, headers=h, json={'item': 'caneta', 'quantidade': 3})
    assert record.status_code == 201, record.text
    assert record.json()['data']['quantidade'] == 3
    assert c.get('/api/data/' + name, headers=h).json()['total'] == 1
    # Never the platform's or another organization's tables.
    platform = 'plataforma_' + uuid4().hex[:6]
    create(c, platform)
    create(c, platform, b)
    assert c.post(f'/api/tables/{platform}/columns', headers=h, json={'name': 'x'}).status_code == 404
    assert c.delete(f'/api/tables/{platform}', headers=h, params={'confirm': platform}).status_code == 404
    assert c.post('/api/tables', headers=h, params={'organization_id': b}, json={'name': 'invasao'}).status_code == 404
    assert c.get('/api/data/' + platform, headers=h, params={'organization_id': b}).status_code == 404
    assert c.delete(f'/api/tables/{name}', headers=h, params={'confirm': name}).status_code == 204


def test_plain_members_still_cannot_build(admin_client):
    c = admin_client
    a = org(c)
    h = member(c, a, {})
    assert c.post('/api/tables', headers=h, json={'name': 'nova_' + uuid4().hex[:6]}).status_code == 403
    user = c.get('/api/users').json()['data'][-1]
    assert c.post('/api/tokens', json={'name': 'x', 'user_id': user['id'], 'admin': True}).status_code == 422


def test_organization_administrator_in_the_panel(admin_client):
    c = admin_client
    a = org(c)
    _, credentials, user_id = org_admin(c, a)
    panel = TestClient(app)
    panel.headers.update({'Origin': 'http://testserver', 'X-SETAPI-CSRF': '1'})
    assert panel.post('/api/auth/login', json=credentials).status_code == 200
    me = panel.get('/api/auth/me').json()
    assert me['org_admin'] is True and me['admin'] is False and me['organization_id'] == a
    name = 'painel_' + uuid4().hex[:6]
    assert panel.post('/api/tables', json={'name': name}).status_code == 201
    assert name in names(c, a)
    # Global-only areas stay closed.
    for path in ('/api/users', '/api/organizations/' + a + '/x', '/api/mail/config', '/api/status'):
        assert panel.get(path).status_code in (403, 404, 405)
    # Backups: only the organization's own, never the platform's or everyone's.
    assert panel.get('/api/backups', params={'all': 'true'}).json()['data'] == [x for x in c.get('/api/backups', params={'organization_id': a}).json()['data']]
    # Revoking the role takes effect immediately.
    assert c.put(f'/api/users/{user_id}/access', json={'tenant_id': a, 'org_admin': False}).status_code == 200
    assert panel.get('/api/auth/me').status_code == 401


def test_promoting_keeps_the_token_and_makes_it_administrative(admin_client):
    c = admin_client
    a, b = org(c), org(c)
    headers = member(c, a, {})
    user_id = c.get('/api/auth/me', headers=headers).json()['id']
    assert c.get('/api/auth/me', headers=headers).json()['org_admin'] is False
    assert c.put(f'/api/users/{user_id}/access', json={'tenant_id': a, 'org_admin': True}).status_code == 200
    # The same token keeps working, now as the organization administrator.
    me = c.get('/api/auth/me', headers=headers).json()
    assert me['org_admin'] is True and me['admin'] is False and me['organization_id'] == a
    name = 'promovido_' + uuid4().hex[:6]
    assert c.post('/api/tables', headers=headers, json={'name': name}).status_code == 201
    assert name in names(c, a)
    # Saving the administrator again is harmless and keeps the token.
    assert c.put(f'/api/users/{user_id}/access', json={'tenant_id': a, 'org_admin': True}).status_code == 200
    assert c.get('/api/auth/me', headers=headers).status_code == 200
    # Moving to another organization ends the token, even as administrator there.
    assert c.put(f'/api/users/{user_id}/access', json={'tenant_id': b, 'org_admin': True}).status_code == 200
    assert c.get('/api/auth/me', headers=headers).status_code == 401


def test_any_token_of_an_organization_administrator_administers_it(admin_client):
    c = admin_client
    a = org(c)
    user = c.post('/api/users', json={'email': uuid4().hex[:8] + '@org.test', 'password': 'Admin-password-123', 'tenant_id': a, 'org_admin': True})
    assert user.status_code == 201, user.text
    token = c.post('/api/tokens', json={'name': 'app', 'user_id': user.json()['id']})
    assert token.status_code == 201, token.text
    headers = {'Authorization': 'Bearer ' + token.json()['token']}
    assert c.get('/api/auth/me', headers=headers).json()['org_admin'] is True
    name = 'direto_' + uuid4().hex[:6]
    assert c.post('/api/tables', headers=headers, json={'name': name, 'columns': [{'name': 'nome', 'type': 'text'}]}).status_code == 201
    assert c.post(f'/api/data/{name}', headers=headers, json={'nome': 'x'}).status_code == 201
    # Still confined to its own organization.
    assert name not in names(c)


def test_demoting_revokes_the_administrative_token(admin_client):
    c = admin_client
    a = org(c)
    headers = member(c, a, {})
    user_id = c.get('/api/auth/me', headers=headers).json()['id']
    assert c.put(f'/api/users/{user_id}/access', json={'tenant_id': a, 'org_admin': True}).status_code == 200
    assert c.put(f'/api/users/{user_id}/access', json={'tenant_id': a, 'org_admin': False}).status_code == 200
    assert c.get('/api/auth/me', headers=headers).status_code == 401


def test_organization_administrator_needs_an_organization(admin_client):
    c = admin_client
    assert c.post('/api/users', json={'email': uuid4().hex[:8] + '@x.test', 'password': 'Password-123456', 'org_admin': True}).status_code == 422
    assert c.post('/api/users', json={'email': uuid4().hex[:8] + '@x.test', 'password': 'Password-123456', 'org_admin': True,
                                      'tenant_id': org(c), 'audience': 'app'}).status_code == 422


def test_global_administrator_can_show_a_token_again(admin_client):
    c = admin_client
    a = org(c)
    user = c.post('/api/users', json={'email': uuid4().hex[:8] + '@org.test', 'password': 'Admin-password-123', 'tenant_id': a, 'org_admin': True}).json()
    created = c.post('/api/tokens', json={'name': 'app', 'user_id': user['id']}).json()
    listed = next(t for t in c.get('/api/tokens').json()['data'] if t['id'] == created['id'])
    assert listed['revealable'] is True and listed['org_admin'] is True
    shown = c.get(f"/api/tokens/{created['id']}/secret")
    assert shown.status_code == 200 and shown.json()['token'] == created['token']
    assert shown.headers['cache-control'] == 'no-store'
    assert any(e['action'] == 'token.reveal' and e['resource'] == str(created['id']) for e in c.get('/api/audit').json()['data'])
    # An API token, even the global administrator's, never reads other tokens.
    global_token = c.post('/api/tokens', json={'name': 'global', 'admin': True}).json()['token']
    api = TestClient(app)
    assert api.get(f"/api/tokens/{created['id']}/secret", headers={'Authorization': 'Bearer ' + global_token}).status_code == 403
    assert api.get(f"/api/tokens/{created['id']}/secret", headers={'Authorization': 'Bearer ' + created['token']}).status_code == 403
    # Tokens from before the feature cannot be shown; revoked ones are gone.
    with db.connection() as conn:
        conn.execute('UPDATE setapi.tokens SET secret_encrypted=NULL WHERE id=%s', (created['id'],))
    assert c.get(f"/api/tokens/{created['id']}/secret").status_code == 409
    assert c.delete(f"/api/tokens/{created['id']}").status_code == 204
    assert c.get(f"/api/tokens/{created['id']}/secret").status_code == 404
    # Login sessions are never stored.
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM setapi.tokens WHERE kind='session' AND secret_encrypted IS NOT NULL").fetchone()['n'] == 0


def test_revoked_token_can_be_deleted(admin_client):
    c = admin_client
    a = org(c)
    user = c.post('/api/users', json={'email': uuid4().hex[:8] + '@org.test', 'password': 'Admin-password-123', 'tenant_id': a, 'org_admin': True}).json()
    created = c.post('/api/tokens', json={'name': 'velho', 'user_id': user['id']}).json()
    # An active token must be revoked first.
    assert c.delete(f"/api/tokens/{created['id']}/permanent").status_code == 409
    assert c.delete(f"/api/tokens/{created['id']}").status_code == 204
    assert c.delete(f"/api/tokens/{created['id']}/permanent").status_code == 204
    assert all(t['id'] != created['id'] for t in c.get('/api/tokens').json()['data'])
    assert c.delete(f"/api/tokens/{created['id']}/permanent").status_code == 404
    assert any(e['action'] == 'token.delete' for e in c.get('/api/audit').json()['data'])
    # Only the global administrator deletes.
    other = c.post('/api/tokens', json={'name': 'outro', 'user_id': user['id']}).json()
    c.delete(f"/api/tokens/{other['id']}")
    assert TestClient(app).delete(f"/api/tokens/{other['id']}/permanent", headers={'Authorization': 'Bearer ' + created['token']}).status_code == 401
