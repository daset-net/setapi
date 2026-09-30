import os
import subprocess
import json
from pathlib import Path
import pytest
import psycopg
from cryptography.fernet import Fernet
from app.backup import encrypt_file,decrypt_file,create_backup


def test_streaming_encryption_tamper_and_truncation(tmp_path):
    key=Fernet.generate_key();source=tmp_path/'source';source.write_bytes(os.urandom(2_100_000))
    encrypted=tmp_path/'backup';encrypt_file(source,encrypted,key)
    target=tmp_path/'result';decrypt_file(encrypted,target,key)
    assert target.read_bytes()==source.read_bytes()
    raw=bytearray(encrypted.read_bytes());raw[100]^=1;encrypted.write_bytes(raw)
    with pytest.raises(Exception):decrypt_file(encrypted,tmp_path/'tampered',key)
    assert not (tmp_path/'tampered').exists()
    encrypt_file(source,encrypted,key);encrypted.write_bytes(encrypted.read_bytes()[:-20])
    with pytest.raises(Exception):decrypt_file(encrypted,tmp_path/'truncated',key)
    assert not (tmp_path/'truncated').exists()
    with pytest.raises(FileExistsError):decrypt_file(encrypted,target,key)
    assert target.read_bytes()==source.read_bytes()


def test_backup_and_restore_real_postgres(admin_client,tmp_path,monkeypatch):
    from app import storage,db
    c=admin_client
    from uuid import uuid4
    table = 'restore_' + uuid4().hex[:10]
    assert c.post('/api/tables', json={'name': table, 'organization_isolated': True, 'columns': [{'name': 'title'}]}).status_code == 201
    with db.connection() as conn:
        from psycopg import sql
        conn.execute(sql.SQL('CREATE POLICY external_policy ON data.{} USING (false)').format(sql.Identifier(table)))
    row=c.post('/api/storages',json={'name':'backup_restore_test','provider':'s3','config':{'bucket':'test','access_key_id':'test','secret_access_key':'test'}}).json()
    saved=tmp_path/'saved.setapi'
    def upload(self,path,key,content_type='application/octet-stream'):
        import shutil
        shutil.copyfile(path,saved);return key
    monkeypatch.setattr(storage.Storage,'upload',upload)
    key,size,checksum=create_backup({'id':'test','storage_id':row['id']})
    assert saved.exists() and size>0 and len(checksum)==64
    from psycopg.conninfo import conninfo_to_dict,make_conninfo
    config=conninfo_to_dict(os.environ['DATABASE_URL']);config['dbname']='postgres'
    with psycopg.connect(make_conninfo(**config),autocommit=True) as conn:
        conn.execute('DROP DATABASE IF EXISTS setapi_restore_test')
        conn.execute('CREATE DATABASE setapi_restore_test')
    config['dbname']='setapi_restore_test';target=make_conninfo(**config)
    env=dict(os.environ,SETAPI_RESTORE_DATABASE_URL=target)
    run=subprocess.run([os.sys.executable,'-m','app.restore',str(saved),'--confirm-database','setapi_restore_test'],env=env,capture_output=True,text=True)
    assert run.returncode==0,run.stdout+run.stderr
    with psycopg.connect(target) as conn:
        assert conn.execute("SELECT count(*) FROM pg_policies WHERE schemaname='data' AND policyname IN ('setapi_read_allow','setapi_read_boundary')").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM pg_policies WHERE tablename=%s AND policyname='external_policy'", (table,)).fetchone()[0] == 1
        assert conn.execute('SELECT count(*) FROM setapi.users').fetchone()[0]>0
        assert conn.execute('SELECT count(*) FROM setapi.tokens WHERE revoked_at IS NULL').fetchone()[0]==0
    from app import postgrest
    if postgrest.enabled():
        bootstrap = subprocess.run([os.sys.executable, '-c', 'from app import db; db.start(); db.stop()'],
                                   env=dict(env, DATABASE_URL=target), capture_output=True, text=True)
        assert bootstrap.returncode == 0, bootstrap.stderr
        with psycopg.connect(target) as conn:
            policies = conn.execute("SELECT roles FROM pg_policies WHERE tablename=%s AND policyname='setapi_read_boundary'", (table,)).fetchone()
            assert policies and postgrest.names()[0] not in policies[0]
    second=subprocess.run([os.sys.executable,'-m','app.restore',str(saved),'--confirm-database','setapi_restore_test'],env=env,capture_output=True,text=True)
    assert second.returncode!=0 and 'empty' in second.stderr


def test_worker_progress_visible_and_failure_recorded(admin_client,monkeypatch):
    from app import worker,db
    c=admin_client
    storage_id=c.get('/api/storages').json()['data'][0]['id']
    with db.connection() as conn:
        conn.execute("UPDATE setapi.backups SET status='failed' WHERE status='queued'")
    job=c.post('/api/backups',json={'storage_id':storage_id}).json()
    def fake_backup(item):
        with db.connection() as conn:
            assert conn.execute('SELECT status FROM setapi.backups WHERE id=%s',(item['id'],)).fetchone()['status']=='running'
        raise RuntimeError('Simulated provider unavailable')
    monkeypatch.setattr(worker,'create_backup',fake_backup)
    assert worker.run_backup() is True
    with db.connection() as conn:
        row=conn.execute('SELECT status,error FROM setapi.backups WHERE id=%s',(job['id'],)).fetchone()
    assert row['status']=='failed' and row['error']


def test_organization_backup_holds_only_its_tables(admin_client,tmp_path,monkeypatch):
    from app import storage,db
    from app.backup import decrypt_file
    import tarfile
    from uuid import uuid4
    c=admin_client
    org=c.post('/api/organizations',json={'name':'Backup '+uuid4().hex[:8]}).json()['id']
    other=c.post('/api/organizations',json={'name':'Outra '+uuid4().hex[:8]}).json()['id']
    for owner in (org,other):
        assert c.post('/api/tables',params={'organization_id':owner},json={'name':'pacientes','columns':[{'name':'nome'}]}).status_code==201
    store=c.post('/api/storages',json={'name':'org_backup','provider':'s3','organization_id':org,'config':{'bucket':'test','access_key_id':'test','secret_access_key':'test'}}).json()
    platform=c.post('/api/storages',json={'name':'plat_backup_'+uuid4().hex[:6],'provider':'s3','config':{'bucket':'test','access_key_id':'test','secret_access_key':'test'}}).json()
    # The organization backup must land in its own storage, never the platform's (and vice versa).
    assert c.post('/api/backups',json={'storage_id':platform['id'],'organization_id':org}).status_code==404
    assert c.post('/api/backups',json={'storage_id':store['id']}).status_code==404
    assert c.post('/api/backup-schedules',json={'storage_id':store['id'],'organization_id':org,'retention':8}).status_code==422
    assert c.post('/api/backup-schedules',json={'storage_id':store['id'],'organization_id':org,'retention':3}).status_code==201
    assert [s['retention'] for s in c.get('/api/backup-schedules',params={'organization_id':org}).json()['data']]==[3]
    assert all(s['organization_id'] is None for s in c.get('/api/backup-schedules').json()['data'])
    assert org in [s['organization_id'] for s in c.get('/api/backup-schedules',params={'all':'true'}).json()['data']]
    saved=tmp_path/'org.setapi'
    def upload(self,path,key,content_type='application/octet-stream'):
        import shutil
        shutil.copyfile(path,saved);return key
    monkeypatch.setattr(storage.Storage,'upload',upload)
    create_backup({'id':'org-test','storage_id':store['id'],'organization_id':org})
    from app.config import settings
    decrypt_file(saved,tmp_path/'bundle.tar',settings().encryption_key)
    with tarfile.open(tmp_path/'bundle.tar') as archive:
        archive.extractall(tmp_path,filter='data')
    config=json.loads((tmp_path/'config.json').read_text())
    assert config['scope']=='organization' and config['organization']['id']==org
    assert [s['name'] for s in config['storages']]==['org_backup']
    listing=subprocess.run(['pg_restore','--list',str(tmp_path/'database.dump')],capture_output=True,text=True).stdout
    with db.connection() as conn:
        mine,theirs=(conn.execute('SELECT table_prefix FROM setapi.organizations WHERE id=%s',(o,)).fetchone()['table_prefix'] for o in (org,other))
    assert mine+'pacientes' in listing and theirs not in listing and 'TABLE setapi ' not in listing and 'TABLE DATA setapi ' not in listing
