from uuid import uuid4
from app import db


def setup(c):
    organizations=[c.post('/api/organizations',json={'name':'School '+uuid4().hex}).json() for _ in range(2)]
    table='t_'+uuid4().hex[:10]
    assert c.post('/api/tables',json={'name':table,'organization_isolated':True,'columns':[{'name':'title'}]}).status_code==201
    users=[]
    for org in organizations:
        credentials={'email':uuid4().hex+'@school.test','password':'Student-password-123'}
        created=c.post('/api/users',json={**credentials,'audience':'app','tenant_id':org['id'],'scopes':{table:['read','create','update','delete']}})
        assert created.status_code==201,created.text
        login=c.post('/api/app-auth/login',json=credentials);assert login.status_code==200
        users.append((credentials,{'Authorization':'Bearer '+login.json()['token']},created.json()['id']))
    return organizations,table,users


def test_organizations_isolate_crud_listing_and_metadata(admin_client):
    c=admin_client;orgs,table,users=setup(c);h1=users[0][1];h2=users[1][1]
    one=c.post('/api/data/'+table,headers=h1,json={'title':'School A'});assert one.status_code==201,one.text
    rid=one.json()['data']['id']
    assert c.get('/api/data/'+table,headers=h2).json()['total']==0
    assert c.get('/api/data/'+table+'/'+rid,headers=h2).status_code==404
    assert c.patch('/api/data/'+table+'/'+rid,headers=h2,json={'title':'leak'}).status_code==404
    assert c.delete('/api/data/'+table+'/'+rid,headers=h2).status_code==404
    assert c.post('/api/data/'+table,headers=h1,json={'title':'forge','organization_id':orgs[1]['id']}).status_code==403
    own=c.get('/api/organizations',headers=h1).json()['data'];assert len(own)==1 and own[0]['id']==orgs[0]['id']
    assert c.get('/api/organizations/'+orgs[1]['id'],headers=h1).status_code==404
    assert c.post('/api/organizations',headers=h1,json={'name':'Unauthorized'}).status_code==403
    assert c.get('/api/users',headers=h1).status_code==403
    assert c.get('/api/storages',headers=h1).status_code==403
    with db.connection() as conn:
        row=conn.execute('SELECT organization_id FROM data.'+table+' WHERE id=%s',(rid,)).fetchone()
    assert str(row['organization_id'])==orgs[0]['id']


def test_disabled_organization_revokes_sessions_and_login(admin_client):
    c=admin_client;orgs,table,users=setup(c);creds,h,_=users[0]
    assert c.patch('/api/organizations/'+orgs[0]['id'],json={'active':False}).status_code==200
    assert c.get('/api/auth/me',headers=h).status_code==401
    assert c.post('/api/app-auth/login',json=creds).status_code==401
    assert c.get('/api/auth/me',headers=users[1][1]).status_code==200
    assert c.patch('/api/organizations/'+orgs[0]['id'],json={'active':True}).status_code==200
    assert c.get('/api/auth/me',headers=h).status_code==401
    assert c.post('/api/app-auth/login',json=creds).status_code==200


def test_global_admin_cannot_be_mixed_with_organization_and_tables_fail_closed(admin_client):
    c=admin_client;orgs,table,users=setup(c)
    assert c.post('/api/users',json={'email':'global-'+uuid4().hex+'@test.example','password':'Strong-password-123','role':'admin','tenant_id':orgs[0]['id']}).status_code==422
    current=c.get('/api/auth/me').json()['id']
    assert c.put('/api/users/'+current+'/access',json={'tenant_id':orgs[0]['id']}).status_code==422
    assert c.put('/api/users/'+users[0][2]+'/access',json={'tenant_id':str(uuid4())}).status_code==422
    # Even an accidentally broad policy cannot expose a shared table to organization users.
    assert c.put('/api/tables/'+table+'/policy',json={'read_fields':['id','title'],'write_fields':['title']}).status_code==200
    assert c.get('/api/data/'+table,headers=users[0][1]).status_code==403
    assert table not in str(c.get('/api/tables',headers=users[0][1]).json())


def test_realtime_isolation_between_organizations(admin_client):
    c=admin_client;orgs,table,users=setup(c)
    from app.worker import publish_events
    with c.websocket_connect('/ws',headers={'origin':'http://testserver'}) as ws:
        ws.send_json({'token':users[0][1]['Authorization'][7:],'tables':[table]})
        assert ws.receive_json()['type']=='ready'
        c.post('/api/data/'+table,headers=users[1][1],json={'title':'Other school'})
        own=c.post('/api/data/'+table,headers=users[0][1],json={'title':'My school'}).json()['data']
        while publish_events():pass
        event=ws.receive_json();assert event['id']==own['id'] and '_row' not in event


def test_organization_api_tokens_inherit_owner_and_cannot_escalate(admin_client):
    c=admin_client;orgs,table,users=setup(c)
    other=c.post('/api/data/'+table,headers=users[1][1],json={'title':'Other organization'}).json()['data']['id']
    token=c.post('/api/tokens',json={'name':'Org integration','user_id':users[0][2],'scopes':{table:['read']}})
    assert token.status_code==201,token.text
    h={'Authorization':'Bearer '+token.json()['token']}
    assert c.get('/api/data/'+table+'/'+other,headers=h).status_code==404
    assert c.get('/api/organizations',headers=h).json()['data'][0]['id']==orgs[0]['id']
    assert c.post('/api/tokens',json={'name':'Escalation','user_id':users[0][2],'admin':True}).status_code==422
    assert c.post('/api/tokens',json={'name':'Unknown table scope','user_id':users[0][2],'scopes':{'unknown_table':['read']}}).status_code==422
    c.patch('/api/organizations/'+orgs[0]['id'],json={'active':False})
    assert c.get('/api/auth/me',headers=h).status_code==401


def test_user_transfer_does_not_carry_old_organization_files(admin_client):
    c=admin_client;orgs,table,users=setup(c)
    store=c.post('/api/storages',json={'name':'Test '+uuid4().hex,'provider':'s3','config':{'bucket':'example','access_key_id':'test','secret_access_key':'test'}}).json()['id']
    with db.connection() as conn:
        file=conn.execute("INSERT INTO setapi.files(storage_id,owner_id,name,object_key,content_type,size,organization_id) VALUES(%s,%s,'Private','unused','text/plain',1,%s) RETURNING id",(store,users[0][2],orgs[0]['id'])).fetchone()['id']
    assert any(f['id']==str(file) for f in c.get('/api/files',headers=users[0][1]).json()['data'])
    assert c.put('/api/users/'+users[0][2]+'/access',json={'tenant_id':orgs[1]['id'],'scopes':{table:['read']}}).status_code==200
    new=c.post('/api/app-auth/login',json=users[0][0]).json()['token']
    h={'Authorization':'Bearer '+new}
    assert all(f['id']!=str(file) for f in c.get('/api/files',headers=h).json()['data'])
    assert c.get('/api/files/'+str(file)+'/download',headers=h).status_code==404
