import os
import tempfile
from pathlib import Path
from uuid import UUID, uuid4
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field
from . import db, storage, platform_settings
from .config import settings
from .security import admin, principal, is_admin, audit, can_manage_storage, storage_manager

router = APIRouter(prefix='/api', tags=['Storage and backups'])


class StorageCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    provider: str
    config: dict
    organization_id: UUID | None = Field(None, description='Organization that owns the connection; administrators only. Others always use their own.')


def name_taken(conn, organization_id, name, exclude=None):
    return conn.execute('SELECT 1 FROM setapi.storages WHERE organization_id IS NOT DISTINCT FROM %s AND name=%s AND id IS DISTINCT FROM %s',
                        (organization_id, name, exclude)).fetchone() is not None


@router.get('/storages')
def list_storages(user=Depends(storage_manager)):
    with db.connection() as conn:
        return {'data': conn.execute('SELECT id,name,provider,organization_id,created_at FROM setapi.storages WHERE %s OR organization_id=%s ORDER BY name',
                                     (is_admin(user), user.get('tenant_id'))).fetchall()}


@router.post('/storages', status_code=201)
def create_storage(body: StorageCreate, user=Depends(storage_manager)):
    storage.validate(body.provider, body.config)
    with db.connection() as conn:
        organization = storage.owner(conn, user, body.organization_id)
        if name_taken(conn, organization, body.name):
            raise HTTPException(409, 'Já existe uma conexão com esse nome.')
        row = conn.execute('INSERT INTO setapi.storages(name,provider,config_encrypted,organization_id) VALUES(%s,%s,%s,%s) RETURNING id,name,provider,organization_id',
            (body.name, body.provider, storage.encrypt(body.config), organization)).fetchone()
        audit(conn, user, 'storage.create', str(row['id']))
    return row


@router.put('/storages/{storage_id}')
def update_storage(storage_id: UUID, body: StorageCreate, user=Depends(storage_manager)):
    storage.validate(body.provider, body.config)
    with db.connection() as conn:
        current = storage.get(conn, storage_id, storage.scope(user))
        if current.provider != body.provider:
            raise HTTPException(409, 'Create a new connection to change provider')
        if name_taken(conn, current.organization_id, body.name, storage_id):
            raise HTTPException(409, 'Já existe uma conexão com esse nome.')
        # Existing file keys must keep referring to the same bucket/folder.
        previous = current.config
        for key in ('bucket', 'endpoint_url', 'folder_id'):
            if previous.get(key) != body.config.get(key):
                raise HTTPException(409, 'Destination cannot change; create a new connection')
        conn.execute('UPDATE setapi.storages SET name=%s,config_encrypted=%s WHERE id=%s', (body.name, storage.encrypt(body.config), storage_id))
        audit(conn, user, 'storage.update', str(storage_id))
    return {'ok': True}


@router.post('/storages/{storage_id}/test')
def test_storage(storage_id: UUID, user=Depends(storage_manager)):
    with db.connection() as conn:
        adapter = storage.get(conn, storage_id, storage.scope(user))
    try:
        adapter.test()
    except Exception:
        raise HTTPException(502, 'Provider connection failed; check credentials, destination and permissions')
    return {'ok': True}


def visible_files(user):
    """SQL filter and parameters for the files this user may see."""
    if is_admin(user):
        return 'true', ()
    if can_manage_storage(user):
        return 'organization_id=%s', (user['tenant_id'],)
    return 'owner_id=%s AND organization_id IS NOT DISTINCT FROM %s', (user['id'], user.get('tenant_id'))


@router.get('/files')
def list_files(user=Depends(principal)):
    where, params = visible_files(user)
    with db.connection() as conn:
        return {'data': conn.execute('SELECT id,storage_id,owner_id,name,size,content_type,created_at FROM setapi.files WHERE ' + where + ' ORDER BY created_at DESC LIMIT 200', params).fetchall()}


@router.post('/files/{storage_id}', status_code=201)
def upload_file(storage_id: UUID, file: UploadFile, user=Depends(storage_manager)):
    # Only panel users upload, and only into their organization's storage; app tokens never do.
    with db.connection() as conn:
        adapter = storage.get(conn, storage_id, storage.scope(user))
    fd, path = tempfile.mkstemp()
    size = 0
    key = None
    try:
        with os.fdopen(fd, 'wb') as stream:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > platform_settings.current()['max_upload_mb'] * 1024 * 1024:
                    raise HTTPException(413, 'File exceeds upload limit')
                stream.write(chunk)
        if not size:
            raise HTTPException(422, 'Empty file')
        file_id = uuid4()
        key = adapter.upload(path, 'uploads/' + str(file_id), file.content_type or 'application/octet-stream')
        with db.connection() as conn:
            row = conn.execute('''INSERT INTO setapi.files(id,storage_id,owner_id,name,object_key,content_type,size,organization_id)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id,name,size''',
                (file_id, storage_id, user['id'], Path(file.filename or 'file').name[:255], key,
                 file.content_type or 'application/octet-stream', size, adapter.organization_id)).fetchone()
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
    where, params = visible_files(user)
    with db.connection() as conn:
        row = conn.execute('SELECT * FROM setapi.files WHERE id=%s AND ' + where, (file_id, *params)).fetchone()
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


BACKUP_ORG = Field(None, description='Organization whose tables are backed up into its own storage; omit for the whole platform.')


class BackupCreate(BaseModel):
    storage_id: UUID
    organization_id: UUID | None = BACKUP_ORG


def backup_storage(conn, storage_id, organization_id):
    # The platform backup is the whole database: only platform storage receives it.
    # An organization backup holds only its tables and goes to that organization's storage.
    if organization_id is not None and not conn.execute('SELECT 1 FROM setapi.organizations WHERE id=%s AND active', (organization_id,)).fetchone():
        raise HTTPException(422, 'Choose an active organization')
    return storage.get(conn, storage_id, organization_id)


@router.post('/backups', status_code=202)
def queue_backup(body: BackupCreate, user=Depends(admin)):
    with db.connection() as conn:
        backup_storage(conn, body.storage_id, body.organization_id)
        pending = conn.execute("SELECT count(*) AS n FROM setapi.backups WHERE status IN ('queued','running')").fetchone()['n']
        if pending >= 5:
            raise HTTPException(429, 'Backup queue is full')
        row = conn.execute('INSERT INTO setapi.backups(storage_id,organization_id) VALUES(%s,%s) RETURNING id,status,organization_id',
                           (body.storage_id, body.organization_id)).fetchone()
        audit(conn, user, 'backup.queue', str(row['id']), {'organization_id': str(body.organization_id) if body.organization_id else None})
    return row


@router.get('/backups')
def list_backups(organization_id: UUID | None = Query(None, description='Backups of this organization; omit for the platform backups.'),
                 all: bool = Query(False, description='Every backup: the platform and all organizations.'), user=Depends(admin)):
    with db.connection() as conn:
        return {'data': conn.execute('SELECT * FROM setapi.backups WHERE %s OR organization_id IS NOT DISTINCT FROM %s ORDER BY created_at DESC LIMIT 200',
                                     (all, organization_id)).fetchall()}


class ScheduleCreate(BaseModel):
    storage_id: UUID
    every_hours: int = Field(default=24, ge=1, le=8760)
    retention: int = Field(default=7, ge=1, le=7, description='How many completed copies to keep, from 1 to 7.')
    organization_id: UUID | None = BACKUP_ORG


@router.post('/backup-schedules', status_code=201)
def create_schedule(body: ScheduleCreate, user=Depends(admin)):
    with db.connection() as conn:
        backup_storage(conn, body.storage_id, body.organization_id)
        row = conn.execute('INSERT INTO setapi.schedules(storage_id,every_hours,retention,organization_id) VALUES(%s,%s,%s,%s) RETURNING *',
            (body.storage_id, body.every_hours, body.retention, body.organization_id)).fetchone()
        audit(conn, user, 'backup.schedule', str(row['id']))
    return row


@router.get('/backup-schedules')
def list_schedules(organization_id: UUID | None = Query(None, description='Schedules of this organization; omit for the platform schedules.'),
                   all: bool = Query(False, description='Every schedule: the platform and all organizations.'), user=Depends(admin)):
    with db.connection() as conn:
        return {'data': conn.execute('SELECT * FROM setapi.schedules WHERE %s OR organization_id IS NOT DISTINCT FROM %s ORDER BY next_run',
                                     (all, organization_id)).fetchall()}


@router.delete('/backup-schedules/{schedule_id}', status_code=204)
def delete_schedule(schedule_id: UUID, user=Depends(admin)):
    with db.connection() as conn:
        conn.execute('DELETE FROM setapi.schedules WHERE id=%s', (schedule_id,))
        audit(conn, user, 'backup.schedule.delete', str(schedule_id))


@router.delete('/files/{file_id}',status_code=204)
def delete_file(file_id:UUID,user=Depends(storage_manager)):
    where,params=visible_files(user)
    with db.connection() as conn:
        row=conn.execute('SELECT * FROM setapi.files WHERE id=%s AND '+where+' FOR UPDATE',(file_id,*params)).fetchone()
        if not row:raise HTTPException(404,'File not found')
        try:storage.get(conn,row['storage_id']).delete(row['object_key'])
        except Exception:raise HTTPException(502,'Provider deletion failed; retry later')
        conn.execute('DELETE FROM setapi.files WHERE id=%s',(file_id,))
        audit(conn,user,'file.delete',str(file_id))


@router.delete('/storages/{storage_id}',status_code=204)
def delete_storage(storage_id:UUID,user=Depends(storage_manager)):
    with db.connection() as conn:
        storage.get(conn,storage_id,storage.scope(user))
        # RESTRICT foreign keys preserve connections referenced by files/backups/schedules.
        conn.execute('DELETE FROM setapi.storages WHERE id=%s',(storage_id,))
        audit(conn,user,'storage.delete',str(storage_id))
