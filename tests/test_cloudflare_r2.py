import hashlib
import httpx
from app import cloudflare_r2 as cf, db, storage

TOKEN = 'cf-token-value-0123456789abcdef'
ACCOUNT = '0123456789abcdef0123456789abcdef'


def fake_cloudflare(monkeypatch, accounts=None, bucket_ok=True, bucket_error=10006):
    calls = []
    def request(method, url, headers=None, **kwargs):
        calls.append((method, url, kwargs))
        assert headers['Authorization'] == 'Bearer ' + TOKEN
        path = url[len(cf.API):]
        if path == '/accounts':
            body = {'success': True, 'result': accounts if accounts is not None else [{'id': ACCOUNT, 'name': 'Conta'}]}
        elif path.endswith('/tokens/verify'):
            body = {'success': path.startswith('/accounts'), 'result': {'id': 'token-id-123', 'status': 'active'}}
        else:
            body = {'success': bucket_ok, 'errors': [] if bucket_ok else [{'code': bucket_error, 'message': 'bucket problem'}]}
        return httpx.Response(200, json=body, request=httpx.Request(method, url))
    monkeypatch.setattr(cf.httpx, 'request', request)
    monkeypatch.setattr(storage.socket, 'getaddrinfo', lambda *a, **k: [(None, None, None, None, ('104.18.0.1', 443))])
    monkeypatch.setattr(storage.Storage, 'test', lambda self: None)
    return calls


def test_r2_from_token_creates_bucket_and_derives_keys(admin_client, monkeypatch):
    calls = fake_cloudflare(monkeypatch)
    response = admin_client.post('/api/integrations/cloudflare/connect', json={'name': 'R2 automático', 'api_token': TOKEN})
    assert response.status_code == 201, response.text
    bucket = response.json()['bucket']
    assert bucket.startswith('setapi-plataforma-')
    assert calls[-1][2]['json'] == {'name': bucket}
    with db.connection() as conn:
        row = conn.execute('SELECT * FROM setapi.storages WHERE id=%s', (response.json()['id'],)).fetchone()
    config = storage.decrypt(row['config_encrypted'])
    assert config == {'bucket': bucket, 'region': 'auto', 'endpoint_url': f'https://{ACCOUNT}.r2.cloudflarestorage.com',
                      'access_key_id': 'token-id-123', 'secret_access_key': hashlib.sha256(TOKEN.encode()).hexdigest()}
    assert TOKEN not in row['config_encrypted'] and row['organization_id'] is None


def test_r2_from_token_errors(admin_client, monkeypatch):
    fake_cloudflare(monkeypatch, accounts=[{'id': ACCOUNT, 'name': 'A'}, {'id': 'b' * 32, 'name': 'B'}])
    response = admin_client.post('/api/integrations/cloudflare/connect', json={'name': 'R2 várias', 'api_token': TOKEN})
    assert response.status_code == 409 and ACCOUNT in response.json()['detail']
    fake_cloudflare(monkeypatch, bucket_ok=False)
    response = admin_client.post('/api/integrations/cloudflare/connect', json={'name': 'R2 sem permissão', 'api_token': TOKEN})
    assert response.status_code == 422 and 'Storage Write' in response.json()['detail']
    # An existing bucket the user named is reused.
    fake_cloudflare(monkeypatch, bucket_ok=False, bucket_error=10073)
    response = admin_client.post('/api/integrations/cloudflare/connect', json={'name': 'R2 existente', 'api_token': TOKEN, 'bucket': 'meu-bucket'})
    assert response.status_code == 201 and response.json()['bucket'] == 'meu-bucket'
    assert admin_client.post('/api/integrations/cloudflare/connect', json={'name': 'R2 ruim', 'api_token': TOKEN, 'bucket': 'Bucket_Ruim'}).status_code == 422
