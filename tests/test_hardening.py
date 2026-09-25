import json
import os
import secrets
from uuid import uuid4
import pytest
from psycopg.types.json import Jsonb
from app import db, accounts, policies
from app.security import issue, hash_password
from app import storage


def name():return 't_'+uuid4().hex[:10]


def member(conn,table,tenant,owner_email=None,audience='app'):
    user=conn.execute("INSERT INTO setapi.users(email,password_hash,role,audience,tenant_id,scopes) VALUES(%s,%s,'member',%s,%s,%s) RETURNING *",(owner_email or uuid4().hex+'@test.example',hash_password('Very-long-test-pass-123'),audience,tenant,Jsonb({table:['read','create','update','delete']}))).fetchone()
    token=issue(conn,user['id'],'test','session',user['scopes'])['token']
    return user,{'Authorization':'Bearer '+token}


def secured(c):
    table=name();tenant=uuid4()
    assert c.post('/api/tables',json={'name':table,'columns':[{'name':'owner','type':'uuid'},{'name':'tenant','type':'uuid'},{'name':'title'},{'name':'secret'}]}).status_code==201
    assert c.put('/api/tables/'+table+'/policy',json={'owner_column':'owner','tenant_column':'tenant','read_fields':['id','title','created_at'],'write_fields':['title']}).status_code==200
    with db.connection() as conn:
        one,h1=member(conn,table,tenant);two,h2=member(conn,table,tenant)
    return table,one,h1,two,h2


def test_no_cross_student_access_and_no_field_leaks(admin_client):
    c=admin_client;table,one,h1,two,h2=secured(c)
    r=c.post('/api/data/'+table,json={'title':'Mine'},headers=h1)
    assert r.status_code==201,r.text
    rid=r.json()['data']['id'];assert set(r.json()['data'])=={'id','title','created_at'}
    for method in ('get','patch','delete'):
        kwargs={'headers':h2}
        if method=='patch':kwargs['json']={'title':'Stolen'}
        assert getattr(c,method)('/api/data/'+table+'/'+rid,**kwargs).status_code==404
    assert c.get('/api/data/'+table,headers=h2).json()['total']==0
    assert c.get('/api/data/'+table,headers=h1).json()['total']==1
    assert c.post('/api/data/'+table,headers=h1,json={'title':'bad','owner':str(two['id'])}).status_code==403
    assert c.patch('/api/data/'+table+'/'+rid,headers=h1,json={'secret':'bad'}).status_code==403
    assert c.get('/api/data/'+table,headers=h1,params={'filter':json.dumps({'secret':'guess'})}).status_code==422
    assert c.get('/api/data/'+table,headers=h1,params={'sort':'secret'}).status_code==403
    metadata=c.get('/api/tables',headers=h1).json()['data']
    assert {x['name'] for x in next(t for t in metadata if t['name']==table)['columns']}=={'id','title','created_at'}
    assert c.get('/api/users',headers=h1).status_code==403
    assert c.get('/openapi.json',headers=h1).status_code==403


def test_tenant_enforced_and_external_writes_emit_private_routes(admin_client):
    c=admin_client;table,one,h1,two,h2=secured(c)
    with db.connection() as conn:
        other,h3=member(conn,table,uuid4())
    c.put('/api/tables/'+table+'/policy',json={'tenant_column':'tenant','read_fields':['id','title'],'write_fields':['title']})
    r=c.post('/api/data/'+table,headers=h1,json={'title':'Organization'}).json()['data']
    assert c.get('/api/data/'+table,headers=h2).json()['total']==1
    assert c.get('/api/data/'+table,headers=h3).json()['total']==0
    from psycopg import sql
    from app.security import authenticate
    with db.connection() as conn:
        conn.execute(sql.SQL('UPDATE data.{} SET title=%s WHERE id=%s').format(sql.Identifier(table)),('External',r['id']))
        event=conn.execute('SELECT event FROM setapi.outbox WHERE table_name=%s ORDER BY id DESC LIMIT 1',(table,)).fetchone()['event']
    assert 'External' not in json.dumps(event)
    assert policies.visible_event(authenticate(h1['Authorization'][7:]),event)
    assert not policies.visible_event(authenticate(h3['Authorization'][7:]),event)
    assert c.delete('/api/tables/'+table+'/columns/tenant',params={'confirm':table+'.tenant'}).status_code==409


def test_policy_missing_denies_members_and_schema_defaults_hidden(admin_client):
    c=admin_client;table=name()
    c.post('/api/tables',json={'name':table,'columns':[{'name':'title'}]})
    with db.connection() as conn:u,h=member(conn,table,None)
    assert c.get('/api/data/'+table,headers=h).status_code==403
    assert table not in str(c.get('/api/tables',headers=h).json())


def test_change_password_revokes_tokens_and_app_cannot_login_panel(admin_client):
    c=admin_client
    with db.connection() as conn:u,h=member(conn,name(),None)
    credentials={'email':u['email'],'password':'Very-long-test-pass-123'}
    assert c.post('/api/auth/login',json=credentials).status_code==401
    login=c.post('/api/app-auth/login',json=credentials)
    assert login.status_code==200
    assert c.post('/api/auth/password',headers=h,json={'current_password':credentials['password'],'password':'New-long-password-456'}).status_code==200
    assert c.get('/api/auth/me',headers=h).status_code==401
    assert c.post('/api/app-auth/login',json=credentials).status_code==401
    credentials['password']='New-long-password-456'
    assert c.post('/api/app-auth/login',json=credentials).status_code==200


def test_mfa_enrollment_replay_and_recovery(admin_client,monkeypatch):
    c=admin_client
    with db.connection() as conn:u,h=member(conn,name(),None)
    password='Very-long-test-pass-123'
    setup=c.post('/api/auth/mfa/setup',headers=h,json={'password':password}).json()
    now=int(accounts.time.time())
    code=accounts.totp(setup['secret'],now//30)
    enable=c.post('/api/auth/mfa/enable',headers=h,json={'password':password,'otp':code})
    assert enable.status_code==200,enable.text
    credentials={'email':u['email'],'password':password,'otp':code}
    assert c.post('/api/app-auth/login',json=credentials).status_code==401 # same time-step cannot replay
    credentials['otp']=enable.json()['recovery_codes'][0]
    assert c.post('/api/app-auth/login',json=credentials).status_code==200
    assert c.post('/api/app-auth/login',json=credentials).status_code==401
    with db.connection() as conn:
        row=conn.execute('SELECT mfa_secret,recovery_hashes FROM setapi.users WHERE id=%s',(u['id'],)).fetchone()
    assert setup['secret'] not in row['mfa_secret']
    assert credentials['otp'] not in str(row['recovery_hashes'])


def test_registration_email_verification_and_password_reset(admin_client,monkeypatch):
    c=admin_client;email=uuid4().hex+'@test.example'
    creds={'email':email,'password':'Very-long-test-pass-123'}
    assert c.post('/api/app-auth/register',json=creds).status_code==403
    monkeypatch.setenv('SETAPI_ALLOW_REGISTRATION','true');monkeypatch.setenv('SETAPI_SMTP_HOST','smtp.example');monkeypatch.setenv('SETAPI_SMTP_FROM','no-reply@example.com')
    assert c.post('/api/app-auth/register',json={**creds,'role':'admin'}).status_code==422
    response=c.post('/api/app-auth/register',json=creds)
    assert response.status_code==202
    assert c.post('/api/app-auth/login',json=creds).status_code==401
    def token():
        with db.connection() as conn:
            row=conn.execute('SELECT payload_encrypted FROM setapi.mail_queue ORDER BY id DESC LIMIT 1').fetchone()
        assert email not in row['payload_encrypted']
        text=storage.decrypt(row['payload_encrypted'])['text']
        return text.split('token=')[1].split()[0]
    verification=token()
    assert c.post('/api/app-auth/verify',json={'token':verification}).status_code==200
    login=c.post('/api/app-auth/login',json=creds);assert login.status_code==200
    h={'Authorization':'Bearer '+login.json()['token']}
    assert c.get('/api/tables',headers=h).json()['data']==[]
    real=c.post('/api/auth/forgot-password',json={'email':email})
    reset=token()
    fake=c.post('/api/auth/forgot-password',json={'email':'unknown@test.example'})
    assert real.json()==fake.json()
    assert c.post('/api/auth/reset-password',json={'token':reset,'password':'Changed-long-password-123'}).status_code==200
    assert c.post('/api/auth/reset-password',json={'token':reset,'password':'Another-long-password-123'}).status_code==400
    assert c.get('/api/auth/me',headers=h).status_code==401


def test_validation_does_not_echo_secrets_and_oauth_cookie(admin_client):
    c=admin_client;secret='SHOULD_NOT_APPEAR'
    r=c.post('/api/auth/reset-password',json={'token':secret,'password':secret,'otp':{'secret':secret}})
    assert r.status_code==422 and secret not in r.text
    r=c.post('/api/auth/login',json={'email':os.environ['SETAPI_ADMIN_EMAIL'],'password':os.environ['SETAPI_ADMIN_PASSWORD']})
    assert 'SameSite=lax' in r.headers['set-cookie'] and 'HttpOnly' in r.headers['set-cookie']
    assert c.get('/openapi.json').status_code==200


def test_ssrf_endpoints_rejected(admin_client):
    for url in ['http://localhost','https://127.0.0.1','https://169.254.169.254','https://setapi_db','https://evil.example']:
        r=admin_client.post('/api/storages',json={'name':name(),'provider':'s3','config':{'bucket':'test','access_key_id':'test','secret_access_key':'test','endpoint_url':url}})
        assert r.status_code==422,r.text


def test_websocket_does_not_deliver_other_student_ids(admin_client):
    c=admin_client;table,one,h1,two,h2=secured(c)
    with c.websocket_connect('/ws',headers={'origin':'http://testserver'}) as ws:
        ws.send_json({'token':h1['Authorization'][7:],'tables':[table]})
        assert ws.receive_json()['type']=='ready'
        c.post('/api/data/'+table,headers=h2,json={'title':'Other student'})
        own=c.post('/api/data/'+table,headers=h1,json={'title':'Mine'}).json()['data']
        from app.worker import publish_events
        publish_events()
        event=ws.receive_json()
        assert event['id']==own['id'] and '_row' not in event


def test_recovery_requires_mfa_and_owner_tenant_update_invalidates_sessions(admin_client,monkeypatch):
    c=admin_client;table,one,h1,two,h2=secured(c)
    assert c.put('/api/users/'+str(one['id'])+'/access',json={'scopes':{table:['read']},'tenant_id':str(uuid4())}).status_code==200
    assert c.get('/api/auth/me',headers=h1).status_code==401
    with db.connection() as conn:
        secret=__import__('base64').b32encode(secrets.token_bytes(20)).decode()
        conn.execute('UPDATE setapi.users SET mfa_secret=%s WHERE id=%s',(storage.encrypt({'secret':secret}),two['id']))
        reset=secrets.token_urlsafe(40)
        conn.execute("INSERT INTO setapi.action_tokens VALUES(%s,%s,'reset',now()+interval '5 minutes')",(accounts.digest(reset),two['id']))
    assert c.post('/api/auth/reset-password',json={'token':reset,'password':'Reset-long-password-123'}).status_code==401
    assert c.get('/api/auth/me',headers=h2).status_code==200


def test_schema_adoption_indexes_and_type_changes_are_transactional(admin_client):
    from psycopg import sql
    c=admin_client;table=name()
    with db.connection() as conn:
        conn.execute(sql.SQL('CREATE TABLE data.{}(title text)').format(sql.Identifier(table)))
        conn.execute(sql.SQL('INSERT INTO data.{} VALUES(%s)').format(sql.Identifier(table)),('123',))
    assert c.get('/api/data/'+table).status_code==409
    assert c.post('/api/tables/'+table+'/adopt',json={'confirm':'wrong'}).status_code==422
    assert c.post('/api/tables/'+table+'/adopt',json={'confirm':table}).status_code==200
    assert c.get('/api/data/'+table).json()['total']==1
    index=c.post('/api/tables/'+table+'/indexes',json={'columns':['title']})
    assert index.status_code==201,index.text
    assert c.delete('/api/tables/'+table+'/indexes/'+index.json()['name']).status_code==204
    assert c.put('/api/tables/'+table+'/columns/title',json={'type':'integer','nullable':True,'confirm':table+'.title'}).status_code==200
    assert c.get('/api/data/'+table).json()['data'][0]['title']==123
    assert c.put('/api/tables/'+table+'/columns/title',json={'type':'uuid','nullable':True,'confirm':table+'.title'}).status_code in (422,503)
    assert c.get('/api/data/'+table).json()['data'][0]['title']==123


def test_totp_rfc_vector_and_request_size(admin_client):
    import base64
    assert accounts.totp(base64.b32encode(b'12345678901234567890').decode(),1)=='287082'
    table=name();admin_client.post('/api/tables',json={'name':table,'columns':[{'name':'text'}]})
    assert admin_client.post('/api/data/'+table,json={'text':'x'*70000}).status_code==413


def test_foreign_keys_cannot_reference_another_owner(admin_client):
    c=admin_client;parent,one,h1,two,h2=secured(c)
    foreign=c.post('/api/data/'+parent,headers=h2,json={'title':'Private'}).json()['data']['id']
    child=name()
    c.post('/api/tables',json={'name':child,'columns':[{'name':'owner','type':'uuid'},{'name':'parent_id','type':'uuid','references':parent}]})
    c.put('/api/tables/'+child+'/policy',json={'owner_column':'owner','read_fields':['id','parent_id'],'write_fields':['parent_id']})
    from app.security import issue
    with db.connection() as conn:
        scopes={parent:['read'],child:['create','read']}
        conn.execute('UPDATE setapi.users SET scopes=%s WHERE id=%s',(Jsonb(scopes),one['id']))
        token=issue(conn,one['id'],'FK test','session',scopes)['token']
    h={'Authorization':'Bearer '+token}
    assert c.post('/api/data/'+child,headers=h,json={'parent_id':foreign}).status_code==403


def test_mail_transport_requires_tls(admin_client,monkeypatch):
    from app import mail
    captured={}
    class SMTP:
        def __init__(self,*args,**kwargs):captured['connected']=True
        def ehlo(self):pass
        def starttls(self,context):captured['tls']=True
        def login(self,*args):captured['login']=True
        def send_message(self,message):assert captured['tls'];captured['sent']=True
        def __enter__(self):return self
        def __exit__(self,*args):pass
    monkeypatch.setenv('SETAPI_SMTP_HOST','smtp.test.invalid');monkeypatch.setenv('SETAPI_SMTP_FROM','test@test.invalid');monkeypatch.setenv('SETAPI_SMTP_PORT','587')
    monkeypatch.setattr(mail.smtplib,'SMTP',SMTP)
    with db.connection() as conn:
        conn.execute('INSERT INTO setapi.mail_queue(payload_encrypted) VALUES(%s)',(storage.encrypt({'to':'test@test.invalid','subject':'Test','text':'No real email is sent'}),))
    mail.send_one();assert captured['sent']
