from urllib.parse import urlsplit, parse_qs
import httpx
import pytest
from app import db, storage, google_oauth as oauth

CLIENT = '123456-test.apps.googleusercontent.com'
SECRET = 'test-client-secret'


def configure(client):
    assert client.put('/api/integrations/google/config', json={'client_id': CLIENT, 'client_secret': SECRET}).status_code == 200


def begin(client, **kwargs):
    response = client.post('/api/integrations/google/connect', json=kwargs)
    assert response.status_code == 200, response.text
    return parse_qs(urlsplit(response.json()['url']).query)


def finish(client, query, **params):
    return client.get('/api/integrations/google/callback', params={'state':query['state'][0], **params}, follow_redirects=False)


def provider(monkeypatch, tokens=None, folder_ok=True):
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs))
        result = tokens if tokens is not None else {'access_token':'access-secret','refresh_token':'refresh-secret','scope':oauth.SCOPE}
        if '/drive/' in url:
            result = {'id':'created-folder'}
        return httpx.Response(200, json=result, request=httpx.Request('POST',url))
    def get(url, **kwargs):
        return httpx.Response(200 if folder_ok else 404, json={'id':'created-folder','mimeType':'application/vnd.google-apps.folder','capabilities':{'canAddChildren':True}}, request=httpx.Request('GET',url))
    monkeypatch.setattr(oauth.httpx,'post',post)
    monkeypatch.setattr(oauth.httpx,'get',get)
    return calls


def test_google_config_encrypted_and_not_exposed(admin_client):
    configure(admin_client)
    data=admin_client.get('/api/integrations/google/config').json()
    assert data['configured'] and data['client_id']==CLIENT
    assert data['redirect_uri']=='http://testserver/api/integrations/google/callback'
    assert SECRET not in str(data)
    with db.connection() as conn:
        value=conn.execute("SELECT value_encrypted FROM setapi.integrations WHERE name='google'").fetchone()['value_encrypted']
    assert SECRET not in value
    assert admin_client.put('/api/integrations/google/config',json={'client_id':CLIENT}).status_code==200
    assert oauth.client_config()['client_secret']==SECRET
    assert admin_client.put('/api/integrations/google/config',json={'client_id':'other.apps.googleusercontent.com'}).status_code==422


def test_google_consent_pkce_callback_and_replay(admin_client,monkeypatch):
    configure(admin_client)
    query=begin(admin_client,name='OAuth integration')
    assert query['scope']==[oauth.SCOPE] and query['access_type']==['offline']
    assert query['code_challenge_method']==['S256']
    assert SECRET not in str(query)
    calls=provider(monkeypatch)
    response=finish(admin_client,query,code='authorization-code')
    assert response.headers['location']=='/?google=connected#storages'
    assert calls[0][1]['data']['code_verifier']
    assert calls[1][1]['json']['mimeType']=='application/vnd.google-apps.folder'
    with db.connection() as conn:
        row=conn.execute("SELECT * FROM setapi.storages WHERE name='OAuth integration'").fetchone()
    saved=storage.decrypt(row['config_encrypted'])
    assert saved['refresh_token']=='refresh-secret' and saved['folder_id']=='created-folder'
    assert 'refresh-secret' not in admin_client.get('/api/storages').text
    assert finish(admin_client,query,code='same-code').headers['location']=='/?google=expired#storages'
    assert len(calls)==2


def test_google_state_bound_to_session_and_csrf(admin_client,monkeypatch):
    configure(admin_client)
    query=begin(admin_client,name='Bound session')
    key='setapi:google:state:'+query['state'][0]
    saved=storage.decrypt(db.cache.get(key));saved['token_id']='different-session'
    db.cache.setex(key,600,storage.encrypt(saved))
    calls=provider(monkeypatch)
    assert finish(admin_client,query,code='code').headers['location']=='/?google=expired#storages'
    assert calls==[] and db.cache.exists(key)
    assert admin_client.post('/api/integrations/google/connect',json={},headers={'Origin':'https://attacker.example'}).status_code==403
    assert finish(admin_client,{'state':['invalid']},code='code').headers['location']=='/?google=expired#storages'
    db.cache.delete(key)


@pytest.mark.parametrize('tokens', [ {'access_token':'x','scope':oauth.SCOPE}, {'access_token':'x','refresh_token':'x','scope':'openid'} ])
def test_google_requires_refresh_token_and_scope(admin_client,monkeypatch,tokens):
    configure(admin_client);query=begin(admin_client,name='Insufficient permissions')
    calls=provider(monkeypatch,tokens=tokens)
    assert finish(admin_client,query,code='code').headers['location']=='/?google=permission#storages'
    assert len(calls)==1


def test_google_cancel_and_expiry_do_not_call_provider(admin_client,monkeypatch):
    configure(admin_client);query=begin(admin_client,name='Cancelled')
    calls=provider(monkeypatch)
    assert finish(admin_client,query,error='access_denied').headers['location']=='/?google=cancelled#storages'
    assert finish(admin_client,query,code='code').headers['location']=='/?google=expired#storages'
    query=begin(admin_client,name='Expired')
    db.cache.delete('setapi:google:state:'+query['state'][0])
    assert finish(admin_client,query,code='code').headers['location']=='/?google=expired#storages'
    assert calls==[]


def test_google_reconnect_preserves_destination_and_checks_account(admin_client,monkeypatch):
    configure(admin_client)
    response=admin_client.post('/api/storages',json={'name':'Reconnect test','provider':'drive','config':{'client_id':CLIENT,'client_secret':SECRET,'refresh_token':'old-refresh','folder_id':'created-folder'}})
    storage_id=response.json()['id']
    calls=provider(monkeypatch,folder_ok=False)
    query=begin(admin_client,storage_id=storage_id)
    assert finish(admin_client,query,code='code').headers['location']=='/?google=failed#storages'
    with db.connection() as conn:
        assert storage.get(conn,storage_id).config['refresh_token']=='old-refresh'
    calls=provider(monkeypatch)
    query=begin(admin_client,storage_id=storage_id)
    assert finish(admin_client,query,code='code').headers['location']=='/?google=connected#storages'
    assert len(calls)==1  # no new folder on reconnect
    with db.connection() as conn:
        saved=storage.get(conn,storage_id).config
    assert saved['folder_id']=='created-folder' and saved['refresh_token']=='refresh-secret'


def test_google_requires_panel_session_and_unique_name(admin_client):
    from app.security import issue
    configure(admin_client)
    with db.connection() as conn:
        user=conn.execute("SELECT id FROM setapi.users WHERE email='test@example.com'").fetchone()
        token=issue(conn,user['id'],'OAuth test API token',admin=True)['token']
    response=admin_client.post('/api/integrations/google/connect',json={},headers={'Authorization':'Bearer '+token})
    assert response.status_code==403
    response=admin_client.post('/api/storages',json={'name':'Duplicate OAuth name','provider':'drive','config':{'client_id':CLIENT,'client_secret':SECRET,'refresh_token':'old','folder_id':'folder'}})
    assert response.status_code==201
    assert admin_client.post('/api/integrations/google/connect',json={'name':'Duplicate OAuth name'}).status_code==409
