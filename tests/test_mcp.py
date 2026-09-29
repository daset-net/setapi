import base64
import re
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from app import storage
from app.main import app


@pytest.fixture
def token(admin_client):
    created = admin_client.post('/api/tokens', json={'name': 'MCP ' + uuid4().hex[:6], 'admin': True})
    assert created.status_code == 201, created.text
    return created.json()['token']


class Mcp:
    def __init__(self, token):
        self.client = TestClient(app)
        self.headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json, text/event-stream'}
        self.ids = 0

    def rpc(self, method, params=None):
        self.ids += 1
        response = self.client.post('/mcp', headers=self.headers, json={'jsonrpc': '2.0', 'id': self.ids, 'method': method, 'params': params or {}})
        assert response.status_code == 200, response.text
        return response.json()

    def call(self, name, **arguments):
        answer = self.rpc('tools/call', {'name': name, 'arguments': arguments})
        assert 'result' in answer, answer
        return answer['result']


def test_handshake_and_one_tool_per_rest_operation(token):
    mcp = Mcp(token)
    init = mcp.rpc('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 't', 'version': '1'}})['result']
    assert init['protocolVersion'] == '2025-06-18' and init['capabilities']['tools'] and init['serverInfo']['name'] == 'setapi'
    assert mcp.rpc('initialize', {'protocolVersion': '1999-01-01'})['result']['protocolVersion'] == '2025-11-25'
    assert mcp.client.post('/mcp', headers=mcp.headers, json={'jsonrpc': '2.0', 'method': 'notifications/initialized'}).status_code == 202
    assert mcp.rpc('ping')['result'] == {}
    tools = mcp.rpc('tools/list')['result']['tools']
    operations = [(m.upper(), p) for p, ops in app.openapi()['paths'].items() for m in ops]
    assert len(tools) == len(operations) == len({t['name'] for t in tools})
    assert all(re.fullmatch(r'[A-Za-z0-9_-]{1,64}', t['name']) and t['inputSchema']['type'] == 'object' for t in tools)
    described = {re.search(r'\((\w+) (\S+)\)$', t['description']).groups() for t in tools}
    assert described == set(operations)
    assert '$ref' not in str(tools)
    names = {t['name'] for t in tools}
    assert {'create_table', 'add_column', 'rename_column', 'edit_column', 'drop_column', 'drop_table',
            'list_records', 'create_record', 'get_record', 'update_record', 'delete_record', 'upload_file', 'download_file'} <= names
    assert mcp.rpc('nothing/here')['error']['code'] == -32601


def test_schema_and_records_end_to_end(token, admin_client):
    mcp = Mcp(token)
    table = 't_mcp_' + uuid4().hex[:8]
    created = mcp.call('create_table', body={'name': table, 'columns': [{'name': 'nome', 'type': 'text', 'nullable': False}, {'name': 'preco', 'type': 'decimal'}]})
    assert created['isError'] is False and created['structuredContent']['status'] == 201
    assert mcp.call('add_column', table=table, body={'name': 'ativo', 'type': 'boolean'})['structuredContent']['status'] == 201
    assert mcp.call('rename_column', table=table, column='preco', body={'name': 'valor'})['isError'] is False
    assert mcp.call('edit_column', table=table, column='valor', body={'type': 'integer', 'nullable': True, 'confirm': table + '.valor'})['isError'] is False
    record = mcp.call('create_record', table=table, body={'nome': 'Caneta', 'valor': 3, 'ativo': True})
    assert record['structuredContent']['status'] == 201
    record_id = record['structuredContent']['body']['data']['id']
    listed = mcp.call('list_records', table=table, filter={'ativo': True}, limit=10)['structuredContent']
    assert listed['status'] == 200 and [r['id'] for r in listed['body']['data']] == [record_id]
    # Same call, same answer as REST.
    assert listed['body'] == admin_client.get(f'/api/data/{table}', params={'filter': '{"ativo": true}', 'limit': 10}).json()
    assert mcp.call('update_record', table=table, record_id=record_id, body={'valor': 5})['structuredContent']['body']['data']['valor'] == 5
    assert mcp.call('get_record', table=table, record_id=record_id)['structuredContent']['body']['data']['nome'] == 'Caneta'
    assert mcp.call('delete_record', table=table, record_id=record_id)['structuredContent'] == {'status': 204, 'body': None}
    wrong = mcp.call('drop_column', table=table, column='ativo', confirm='errado')
    assert wrong['isError'] is True and wrong['structuredContent']['status'] == 422
    assert mcp.call('drop_column', table=table, column='ativo', confirm=table + '.ativo')['isError'] is False
    assert mcp.call('drop_table', table=table, confirm=table)['structuredContent']['status'] == 204
    assert table not in str(admin_client.get('/api/tables').json())


def test_rest_errors_and_argument_errors_come_back_as_tool_errors(token):
    mcp = Mcp(token)
    record_id = str(uuid4())
    missing = mcp.call('get_record', table='does_not_exist', record_id=record_id)
    rest = TestClient(app).get('/api/data/does_not_exist/' + record_id, headers={'Authorization': 'Bearer ' + token})
    assert missing['isError'] is True and missing['structuredContent'] == {'status': rest.status_code, 'body': rest.json()}
    invalid = mcp.call('create_table', body={'name': 'Nome Invalido'})
    assert invalid['isError'] is True and invalid['structuredContent']['status'] == 422
    assert mcp.call('create_table', body={}, surprise=1)['isError'] is True
    assert mcp.call('get_record', table='x')['isError'] is True
    assert mcp.rpc('tools/call', {'name': 'not_a_tool', 'arguments': {}})['error']['code'] == -32602


def test_token_permissions_are_the_rest_permissions(admin_client):
    org = admin_client.post('/api/organizations', json={'name': 'Org ' + uuid4().hex}).json()
    member = admin_client.post('/api/users', json={'email': uuid4().hex[:8] + '@org.test', 'password': 'Member-password-123', 'tenant_id': org['id']}).json()
    token = admin_client.post('/api/tokens', json={'name': 'member', 'user_id': member['id']}).json()['token']
    mcp = Mcp(token)
    denied = mcp.call('create_table', body={'name': 't_' + uuid4().hex[:8]})
    assert denied['isError'] is True and denied['structuredContent']['status'] == 403
    assert mcp.call('get_me')['structuredContent']['body']['organization_id'] == org['id']


def test_authentication_origin_and_transport(token):
    client = TestClient(app)
    body = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}
    assert client.post('/mcp', json=body).status_code == 401
    assert client.post('/mcp', json=body, headers={'Authorization': 'Bearer set_invalid'}).status_code == 401
    headers = {'Authorization': 'Bearer ' + token}
    assert client.post('/mcp', json=body, headers={**headers, 'Origin': 'https://attacker.example'}).status_code == 403
    assert client.post('/mcp', content=b'{not json', headers={**headers, 'Content-Type': 'application/json'}).json()['error']['code'] == -32700
    assert client.get('/mcp', headers=headers).status_code == 405
    batch = client.post('/mcp', headers=headers, json=[{'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}, {'jsonrpc': '2.0', 'method': 'notifications/initialized'}]).json()
    assert batch == [{'jsonrpc': '2.0', 'id': 1, 'result': {}}]


def test_files_travel_as_base64(token, admin_client, monkeypatch):
    stored = {}
    monkeypatch.setattr(storage.Storage, 'upload', lambda self, path, key, content_type='': stored.update({key: open(path, 'rb').read()}) or key)
    monkeypatch.setattr(storage.Storage, 'download', lambda self, key, path: open(path, 'wb').write(stored[key]))
    monkeypatch.setattr(storage.Storage, 'delete', lambda self, key: stored.pop(key))
    store = admin_client.post('/api/storages', json={'name': 'MCP ' + uuid4().hex[:6], 'provider': 's3', 'config': {'bucket': 'b', 'access_key_id': 'a', 'secret_access_key': 's'}}).json()['id']
    mcp = Mcp(token)
    content = 'relatório,valor\nx,1\n'.encode()
    uploaded = mcp.call('upload_file', storage_id=store, body={'file': {'filename': 'relatorio.csv', 'content_base64': base64.b64encode(content).decode(), 'content_type': 'text/csv'}})
    assert uploaded['structuredContent']['status'] == 201, uploaded
    file_id = uploaded['structuredContent']['body']['id']
    downloaded = mcp.call('download_file', file_id=file_id)['structuredContent']['body']
    assert base64.b64decode(downloaded['content_base64']) == content and downloaded['filename'] == 'relatorio.csv'
    assert mcp.call('delete_file', file_id=file_id)['structuredContent']['status'] == 204
