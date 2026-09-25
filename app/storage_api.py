import os
import tempfile
from pathlib import Path
from uuid import UUID, uuid4
from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field
from . import db, storage
from .config import settings
from .security import admin, principal, is_admin, audit

router = APIRouter(prefix='/api', tags=['Storage and backups'])


class StorageCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    provider: str
    config: dict


@router.get('/storages')
def list_storages(user=Depends(admin)):
    with db.connection() as conn:
        return {'data': conn.execute('SELECT id,name,provider,created_at FROM setapi.storages ORDER BY name').fetchall()}


@router.post('/storages', status_code=201)
def create_storage(body: StorageCreate, user=Depends(admin)):
    storage.validate(body.provider, body.config)
    with db.connection() as conn:
        row = conn.execute('INSERT INTO setapi.storages(name,provider,config_encrypted) VALUES(%s,%s,%s) RETURNING id,name,provider',
            (body.name, body.provider, storage.encrypt(body.config))).fetchone()
        audit(conn, user, 'storage.create', str(row['id']))
    return row


@router.put('/storages/{storage_id}')
def update_storage(storage_id: UUID, body: StorageCreate, user=Depends(admin)):
    storage.validate(body.provider, body.config)
    with db.connection() as conn:
        existing = conn.execute('SELECT provider FROM setapi.storages WHERE id=%s', (storage_id,)).fetchone()
        if not existing:
            raise HTTPException(404, 'Storage not found')
        if existing['provider'] != body.provider:
            raise HTTPException(409, 'Create a new connection to change provider')
        # Existing file keys must keep referring to the same bucket/folder.
        previous = storage.get(conn, storage_id).config
        for key in ('bucket', 'endpoint_url', 'folder_id'):
            if previous.get(key) != body.config.get(key):
                raise HTTPException(409, 'Destination cannot change; create a new connection')
        conn.execute('UPDATE setapi.storages SET name=%s,config_encrypted=%s WHERE id=%s', (body.name, storage.encrypt(body.config), storage_id))
        audit(conn, user, 'storage.update', str(storage_id))
    return {'ok': True}


@router.post('/storages/{storage_id}/test')
def test_storage(storage_id: UUID, user=Depends(admin)):
    with db.connection() as conn:
        adapter = storage.get(conn, storage_id)
    try:
        adapter.test()
    except Exception:
        raise HTTPException(502, 'Provider connection failed; check credentials, destination and permissions')
    return {'ok': True}


@router.get('/files')
def list_files(user=Depends(principal)):
    with db.connection() as conn:
        return {'data': conn.execute('SELECT id,storage_id,owner_id,name,size,content_type,created_at FROM setapi.files WHERE (%s OR owner_id=%s) ORDER BY created_at DESC LIMIT 200', (is_admin(user), user['id'])).fetchall()}


@router.post('/files/{storage_id}', status_code=201)
def upload_file(storage_id: UUID, file: UploadFile, user=Depends(admin)):
    # Upload is administrative in v0.1; app tokens cannot consume arbitrary storage.
    with db.connection() as conn:
        adapter = storage.get(conn, storage_id)
    fd, path = tempfile.mkstemp()
    size = 0
    key = None
    try:
        with os.fdopen(fd, 'wb') as stream:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > settings().max_upload_mb * 1024 * 1024:
                    raise HTTPException(413, 'File exceeds upload limit')
                stream.write(chunk)
        if not size:
            raise HTTPException(422, 'Empty file')
        file_id = uuid4()
        key = adapter.upload(path, 'uploads/' + str(file_id), file.content_type or 'application/octet-stream')
        with db.connection() as conn:
            row = conn.execute('''INSERT INTO setapi.files(id,storage_id,owner_id,name,object_key,content_type,size)
                VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id,name,size''',
                (file_id, storage_id, user['id'], Path(file.filename or 'file').name[:255], key,
                 file.content_type or 'application/octet-stream', size)).fetchone()
            audit(conn, user, 'file.upload', str(file_id))
        return row
    except Exception:
        if key:
            try:adapter.delete(key)
            except Exception:
                import logging
                logging.error('Upload compensation failed; review orphaned objects in storage')
        raise
    finally:
        os.unlink(path)
        file.file.close()


@router.get('/files/{file_id}/download')
def download_file(file_id: UUID, user=Depends(principal)):
    with db.connection() as conn:
        row = conn.execute('SELECT * FROM setapi.files WHERE id=%s AND (%s OR owner_id=%s)', (file_id, is_admin(user), user['id'])).fetchone()
        if not row:
            raise HTTPException(404, 'File not found')
        adapter = storage.get(conn, row['storage_id'])
    fd, path = tempfile.mkstemp()
    os.close(fd)
    try:
        adapter.download(row['object_key'], path)
    except Exception:
        os.unlink(path)
        raise HTTPException(502, 'Download failed')
    return FileResponse(path, filename=row['name'], media_type='application/octet-stream', background=BackgroundTask(os.unlink, path))


class BackupCreate(BaseModel):
    storage_id: UUID


@router.post('/backups', status_code=202)
def queue_backup(body: BackupCreate, user=Depends(admin)):
    with db.connection() as conn:
        storage.get(conn, body.storage_id)
        pending = conn.execute("SELECT count(*) AS n FROM setapi.backups WHERE status IN ('queued','running')").fetchone()['n']
        if pending >= 5:
            raise HTTPException(429, 'Backup queue is full')
        row = conn.execute('INSERT INTO setapi.backups(storage_id) VALUES(%s) RETURNING id,status', (body.storage_id,)).fetchone()
        audit(conn, user, 'backup.queue', str(row['id']))
    return row


@router.get('/backups')
def list_backups(user=Depends(admin)):
    with db.connection() as conn:
        return {'data': conn.execute('SELECT * FROM setapi.backups ORDER BY created_at DESC LIMIT 100').fetchall()}


class ScheduleCreate(BaseModel):
    storage_id: UUID
    every_hours: int = Field(default=24, ge=1, le=8760)
    retention: int = Field(default=7, ge=1, le=365)


@router.post('/backup-schedules', status_code=201)
def create_schedule(body: ScheduleCreate, user=Depends(admin)):
    with db.connection() as conn:
        storage.get(conn, body.storage_id)
        row = conn.execute('INSERT INTO setapi.schedules(storage_id,every_hours,retention) VALUES(%s,%s,%s) RETURNING *',
            (body.storage_id, body.every_hours, body.retention)).fetchone()
        audit(conn, user, 'backup.schedule', str(row['id']))
    return row


@router.get('/backup-schedules')
def list_schedules(user=Depends(admin)):
    with db.connection() as conn:
        return {'data': conn.execute('SELECT * FROM setapi.schedules ORDER BY next_run').fetchall()}


@router.delete('/backup-schedules/{schedule_id}', status_code=204)
def delete_schedule(schedule_id: UUID, user=Depends(admin)):
    with db.connection() as conn:
        conn.execute('DELETE FROM setapi.schedules WHERE id=%s', (schedule_id,))
        audit(conn, user, 'backup.schedule.delete', str(schedule_id))


@router.delete('/files/{file_id}',status_code=204)
def delete_file(file_id:UUID,user=Depends(admin)):
    with db.connection() as conn:
        row=conn.execute('SELECT * FROM setapi.files WHERE id=%s FOR UPDATE',(file_id,)).fetchone()
        if not row:raise HTTPException(404,'File not found')
        try:storage.get(conn,row['storage_id']).delete(row['object_key'])
        except Exception:raise HTTPException(502,'Provider deletion failed; retry later')
        conn.execute('DELETE FROM setapi.files WHERE id=%s',(file_id,))
        audit(conn,user,'file.delete',str(file_id))


@router.delete('/storages/{storage_id}',status_code=204)
def delete_storage(storage_id:UUID,user=Depends(admin)):
    with db.connection() as conn:
        # RESTRICT foreign keys preserve connections referenced by files/backups/schedules.
        conn.execute('DELETE FROM setapi.storages WHERE id=%s',(storage_id,))
        audit(conn,user,'storage.delete',str(storage_id))
