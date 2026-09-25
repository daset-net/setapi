import json
from uuid import uuid4
from app import db
from app.worker import publish_events


def unique():
    return 't_' + uuid4().hex[:12]


def create_table(client, name):
    r=client.post('/api/tables',json={'name':name,'columns':[{'name':'name','type':'text','nullable':False,'unique':True},{'name':'metadata','type':'json'}]})
    assert r.status_code==201,r.text


def test_anonymous_and_csrf(client):
    assert client.get('/api/tables').status_code==401
    assert client.post('/api/tables',json={'name':'forbidden'}).status_code==403


def test_crud_permissions_revocation(admin_client):
    c=admin_client;name=unique();create_table(c,name)
    created=c.post('/api/data/'+name,json={'name':'Maria','metadata':{'active':True}})
    assert created.status_code==201,created.text
    record=created.json()['data'];rid=record['id']
    assert c.post('/api/data/'+name,json={'name':'Maria'}).status_code==409
    assert c.post('/api/data/'+name,json={'id':str(uuid4()),'name':'bad'}).status_code==422
    assert c.post('/api/data/'+name,json={'metadata':{}}).status_code==422
    assert c.patch(f'/api/data/{name}/{rid}',json={'name':'Ana'}).status_code==200
    assert c.get('/api/data/'+name,params={'filter':json.dumps({'name':'Ana'})}).json()['total']==1
    assert c.get('/api/data/'+name,params={'sort':'name; DROP SCHEMA data CASCADE'}).status_code==422
    assert c.get('/api/data/'+name,params={'limit':1000}).status_code==422
    token=c.post('/api/tokens',json={'name':'Read only','scopes':{name:['read']}}).json()
    headers={'Authorization':'Bearer '+token['token']}
    assert c.get('/api/data/'+name,headers=headers).status_code==200
    assert c.post('/api/data/'+name,headers=headers,json={'name':'denied'}).status_code==403
    assert c.get('/api/users',headers=headers).status_code==403
    assert 'digest' not in c.get('/api/tokens').text
    assert token['token'] not in c.get('/api/tokens').text
    assert c.delete('/api/tokens/'+token['id']).status_code==204
    assert c.get('/api/data/'+name,headers=headers).status_code==401
    assert c.delete(f'/api/data/{name}/{rid}').status_code==204
    assert c.get(f'/api/data/{name}/{rid}').status_code==404


def test_schema_transaction_and_relationships(admin_client):
    c=admin_client;parent=unique();child=unique();create_table(c,parent)
    assert c.post('/api/tables',json={'name':'x;drop table users','columns':[]}).status_code==422
    assert c.post('/api/tables',json={'name':unique(),'columns':[{'name':'id'}]}).status_code==422
    assert c.post('/api/tables',json={'name':child,'columns':[{'name':'parent_id','type':'uuid','references':parent}]}).status_code==201
    assert c.delete('/api/tables/'+parent,params={'confirm':parent}).status_code==409
    assert c.post('/api/tables/'+child+'/columns',json={'name':'title'}).status_code==201
    assert c.patch('/api/tables/'+child+'/columns/title',json={'name':'description'}).status_code==200
    assert c.delete('/api/tables/'+child+'/columns/description',params={'confirm':'wrong'}).status_code==422
    assert c.delete('/api/tables/'+child+'/columns/description',params={'confirm':child+'.description'}).status_code==204
    assert c.delete('/api/tables/'+child,params={'confirm':child}).status_code==204
    assert c.delete('/api/tables/'+parent,params={'confirm':parent}).status_code==204


def test_outbox_rollback_and_retry(admin_client,monkeypatch):
    c=admin_client;name=unique();create_table(c,name)
    c.post('/api/data/'+name,json={'name':'first'})
    with db.connection() as conn:
        before=conn.execute('SELECT count(*) AS n FROM setapi.outbox').fetchone()['n']
    c.post('/api/data/'+name,json={'name':'first'})
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) AS n FROM setapi.outbox').fetchone()['n']==before
    original=db.cache.publish
    def fail(*args,**kwargs):raise ConnectionError('offline')
    monkeypatch.setattr(db.cache,'publish',fail)
    import pytest
    with pytest.raises(ConnectionError):publish_events()
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) AS n FROM setapi.outbox').fetchone()['n']==before
    monkeypatch.setattr(db.cache,'publish',original)
    assert publish_events()>=before


def test_websocket_scope_and_delivery(admin_client):
    c=admin_client;name=unique();create_table(c,name)
    token=c.post('/api/tokens',json={'name':'Live','scopes':{name:['read']}}).json()
    with c.websocket_connect('/ws',headers={'origin':'http://testserver'}) as ws:
        ws.send_json({'token':token['token'],'tables':[name]})
        assert ws.receive_json()['type']=='ready'
        db.cache.publish('setapi:events',json.dumps({'table':name,'id':'example','operation':'updated','event_id':999}))
        assert ws.receive_json()['event_id']==999
        c.delete('/api/tokens/'+token['id'])
        db.cache.publish('setapi:events',json.dumps({'table':name,'id':'example','operation':'updated','event_id':1000}))
        from starlette.websockets import WebSocketDisconnect
        import pytest
        with pytest.raises(WebSocketDisconnect):ws.receive_json()


def test_storage_secrets_never_returned(admin_client):
    c=admin_client
    r=c.post('/api/storages',json={'name':unique(),'provider':'s3','config':{'bucket':'example','access_key_id':'TEST_ACCESS','secret_access_key':'TEST_SECRET'}})
    assert r.status_code==201,r.text
    listing=c.get('/api/storages')
    assert 'TEST_SECRET' not in listing.text and 'TEST_ACCESS' not in listing.text
    with db.connection() as conn:
        encrypted=conn.execute('SELECT config_encrypted FROM setapi.storages WHERE id=%s',(r.json()['id'],)).fetchone()['config_encrypted']
        assert 'TEST_SECRET' not in encrypted
    assert c.post('/api/backups',json={'storage_id':r.json()['id']}).status_code==202
    assert c.post('/api/backup-schedules',json={'storage_id':r.json()['id'],'every_hours':24,'retention':7}).status_code==201


def test_reused_table_does_not_inherit_scopes(admin_client):
    c=admin_client;name=unique();create_table(c,name)
    token=c.post('/api/tokens',json={'name':'Old integration','scopes':{name:['read']}}).json()
    c.delete('/api/tables/'+name,params={'confirm':name})
    create_table(c,name)
    assert c.get('/api/data/'+name,headers={'Authorization':'Bearer '+token['token']}).status_code==403


def test_authenticated_cookie_csrf_and_request_limits(admin_client):
    c=admin_client
    assert c.post('/api/tables',headers={'Origin':'https://untrusted.example'},json={'name':unique()}).status_code==403
    assert c.post('/api/tables',content=b'x'*(1024*1024+1),headers={'Content-Type':'application/json'}).status_code==413
    def chunks():
        yield b'{"name":"'
        for _ in range(20):
            yield b'a'*65536
        yield b'"}'
    response=c.post('/api/tables',content=chunks(),headers={'Content-Type':'application/json'})
    assert response.status_code==413,response.text
