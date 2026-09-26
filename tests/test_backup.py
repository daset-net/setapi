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
