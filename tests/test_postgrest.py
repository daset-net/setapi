"""Exercise the real binary and PostgreSQL RLS, not a mocked upstream."""
import json
from uuid import uuid4
import httpx
import pytest
from psycopg import sql
from app import db, postgrest, policies
from app.security import authenticate
from test_organizations import setup


def test_filter_grammar_and_pagination_compatibility(admin_client):
    c = admin_client
    table = 't_' + uuid4().hex[:12]
    columns = [{'name': name} for name in ('select', 'or', 'limit', 'title')]
    columns += [{'name': 'payload', 'type': 'json'}, {'name': 'flag', 'type': 'boolean'}]
    response = c.post('/api/tables', json={'name': table, 'columns': columns})
    assert response.status_code == 201, response.text
    text = 'a,"\\x).or.(id.not.is.null)'
    body = {'select': text, 'or': 'a,b', 'limit': '0', 'title': None, 'payload': {'x': [1, 'quoted"value']}, 'flag': True}
    assert c.post('/api/data/' + table, json=body).status_code == 201
    assert c.post('/api/data/' + table, json={'select': 'another'}).status_code == 201
    for key, value in body.items():
        response = c.get('/api/data/' + table, params={'filter': json.dumps({key: value})})
        assert response.status_code == 200, response.text
        # Both rows have null title; other conditions identify exactly one row.
        assert response.json()['total'] == (2 if key == 'title' else 1)
    response = c.get('/api/data/' + table, params={'offset': 100, 'limit': 1})
    assert response.status_code == 200, response.text
    assert response.json()['data'] == [] and response.json()['total'] == 2


@pytest.mark.skipif(not postgrest.enabled(), reason='Requires the real PostgREST engine')
def test_rls_without_gateway_filters_and_revocation(admin_client):
    c = admin_client
    organizations, table, users = setup(c)
    for index, user in enumerate(users):
        assert c.post('/api/data/' + table, headers=user[1], json={'title': str(index)}).status_code == 201
    # Warm schema cache through the public gateway before exercising internal REST.
    response = c.get('/api/data/' + table, headers=users[0][1])
    assert response.status_code == 200, response.text
    user = authenticate(users[0][1]['Authorization'][7:])
    with db.connection() as conn:
        rule = policies.policy(conn, table, user)
        role = conn.execute('SELECT rolsuper,rolbypassrls,rolcanlogin FROM pg_roles WHERE rolname=%s', (postgrest.names()[0],)).fetchone()
        assert not any(role.values())
        assert not conn.execute("SELECT has_table_privilege(%s,'setapi.tokens','SELECT') AS allowed", (postgrest.names()[0],)).fetchone()['allowed']
    headers = {'Authorization': 'Bearer ' + postgrest.jwt(user, table, rule)}
    url = 'http://127.0.0.1:3000/' + table
    assert httpx.get(url).status_code == 401
    response = httpx.get(url, headers=headers)
    assert response.status_code == 200, response.text
    assert [row['title'] for row in response.json()] == ['0']
    assert httpx.post(url, headers=headers, json={'title': 'forged'}).status_code == 403
    forged = dict(user, tenant_id=organizations[1]['id'])
    assert httpx.get(url, headers={'Authorization': 'Bearer ' + postgrest.jwt(forged, table, rule)}).status_code == 403
    forged = dict(user, role='admin', admin=True, audience='panel', tenant_id=None)
    assert httpx.get(url, headers={'Authorization': 'Bearer ' + postgrest.jwt(forged, table, None)}).status_code == 403
    # Revocation checked in PostgreSQL even with a still-valid internally signed JWT.
    with db.connection() as conn:
        conn.execute('UPDATE setapi.tokens SET revoked_at=now() WHERE id=%s', (user['token_id'],))
    assert httpx.get(url, headers=headers).status_code == 403


@pytest.mark.skipif(not postgrest.enabled(), reason='Requires the real PostgREST engine')
def test_policy_changes_invalidate_inflight_internal_claims(admin_client):
    c = admin_client
    _, table, users = setup(c)
    assert c.get('/api/data/' + table, headers=users[0][1]).status_code == 200
    user = authenticate(users[0][1]['Authorization'][7:])
    with db.connection() as conn:
        rule = policies.policy(conn, table, user)
    headers = {'Authorization': 'Bearer ' + postgrest.jwt(user, table, rule)}
    assert c.put('/api/tables/' + table + '/policy', json={'tenant_column': 'organization_id', 'read_fields': ['id'], 'write_fields': []}).status_code == 200
    assert httpx.get('http://127.0.0.1:3000/' + table, headers=headers).status_code == 403
    response = c.get('/api/data/' + table, headers=users[0][1])
    assert response.status_code == 200


def test_engine_unavailable_never_falls_back_to_privileged_reads(admin_client, monkeypatch):
    from app.security import authenticate
    user = authenticate(admin_client.cookies.get('setapi_session'))
    def unavailable(*args, **kwargs):
        raise httpx.ConnectError('internal diagnostic must never reach caller')
    monkeypatch.setattr(postgrest.HTTP, 'stream', unavailable)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as raised:
        postgrest.read(user, 'missing', None, ['id'], {}, 'id', 1, 0, False)
    assert raised.value.status_code == 503
    assert 'internal diagnostic' not in raised.value.detail


@pytest.mark.skipif(not postgrest.enabled(), reason='Requires the real PostgREST engine')
def test_owner_rls_and_connection_context_reset(admin_client):
    from test_hardening import secured
    c = admin_client
    table, _, first, _, second = secured(c)
    for index, headers in enumerate((first, second)):
        assert c.post('/api/data/' + table, headers=headers, json={'title': str(index)}).status_code == 201
    assert c.get('/api/data/' + table, headers=first).status_code == 200
    with httpx.Client(base_url='http://127.0.0.1:3000') as connection:
        for index in (0, 1, 0, 1):
            user = authenticate((first, second)[index]['Authorization'][7:])
            with db.connection() as conn:
                rule = policies.policy(conn, table, user)
            response = connection.get('/' + table, headers={'Authorization': 'Bearer ' + postgrest.jwt(user, table, rule)})
            assert response.status_code == 200, response.text
            assert [row['title'] for row in response.json()] == [str(index)]
    with db.connection() as conn:
        conn.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(postgrest.names()[0])))
        assert conn.execute(sql.SQL('SELECT count(*) AS n FROM data.{}').format(sql.Identifier(table))).fetchone()['n'] == 0
