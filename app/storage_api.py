import os
import tempfile
from pathlib import Path
from uuid import UUID, uuid4
from psycopg import errors
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field
from . import db, storage, platform_settings, tables
from .config import settings
from .security import admin, builder, principal, is_admin, audit, can_manage_storage, storage_manager

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
def list_files(storage_id: UUID | None = Query(None, description='Only files of this storage connection.'),
               folder_id: UUID | None = Query(None, description='Only files inside this folder.'),
               root: bool = Query(False, description='Only files outside any folder (with storage_id).'),
               limit: int = Query(200, ge=1, le=1000), offset: int = Query(0, ge=0), user=Depends(principal)):
    where, params = visible_files(user)
    clauses, values = [where], list(params)
    for column, value in (('storage_id', storage_id), ('folder_id', folder_id)):
        if value is not None:
            clauses.append(column + '=%s')
            values.append(value)
    if root and folder_id is None:
        clauses.append('folder_id IS NULL')
    with db.connection() as conn:
        return {'data': conn.execute('SELECT id,storage_id,folder_id,owner_id,name,size,content_type,created_at FROM setapi.files WHERE ' + ' AND '.join(clauses)
                                     + ' ORDER BY lower(name), created_at DESC LIMIT %s OFFSET %s', (*values, limit, offset)).fetchall()}


# Folders are rows in the database: renaming and moving never touch the provider.
FOLDER_FIELDS = 'id,storage_id,parent_id,name,created_at'


def visible_folder(conn, user, folder_id, storage_id=None):
    row = conn.execute('SELECT * FROM setapi.folders WHERE id=%s', (folder_id,)).fetchone()
    scope = storage.scope(user)
    if not row or (scope is not storage.ANY and row['organization_id'] != scope) or (storage_id and row['storage_id'] != storage_id):
        raise HTTPException(404, 'Folder not found')
    return row


def folder_name(value):
    value = (value or '').strip()
    if not 1 <= len(value) <= 255 or '/' in value or value in ('.', '..'):
        raise HTTPException(422, 'Nome inválido: use de 1 a 255 caracteres, sem "/".')
    return value


class FolderCreate(BaseModel):
    storage_id: UUID
    name: str = Field(max_length=255)
    parent_id: UUID | None = Field(None, description='Parent folder; omit for the top level of the storage.')


class FolderUpdate(BaseModel):
    name: str | None = Field(None, max_length=255)
    parent_id: UUID | None = Field(None, description='New parent folder; send null to move to the top level.')


@router.get('/folders')
def list_folders(storage_id: UUID = Query(description='Storage connection.'),
                 parent_id: UUID | None = Query(None, description='Only direct children of this folder.'),
                 root: bool = Query(False, description='Only top-level folders.'), user=Depends(storage_manager)):
    """Folders of a storage connection. Without filters, every folder, to build the tree."""
    with db.connection() as conn:
        storage.get(conn, storage_id, storage.scope(user))
        clauses, values = ['storage_id=%s'], [storage_id]
        if parent_id:
            clauses.append('parent_id=%s'); values.append(parent_id)
        elif root:
            clauses.append('parent_id IS NULL')
        return {'data': conn.execute('SELECT ' + FOLDER_FIELDS + ' FROM setapi.folders WHERE ' + ' AND '.join(clauses) + ' ORDER BY lower(name)', values).fetchall()}


@router.post('/folders', status_code=201)
def create_folder(body: FolderCreate, user=Depends(storage_manager)):
    name = folder_name(body.name)
    with db.connection() as conn:
        adapter = storage.get(conn, body.storage_id, storage.scope(user))
        if body.parent_id:
            visible_folder(conn, user, body.parent_id, body.storage_id)
        try:
            with conn.transaction():
                row = conn.execute('INSERT INTO setapi.folders(storage_id,organization_id,parent_id,name) VALUES(%s,%s,%s,%s) RETURNING ' + FOLDER_FIELDS,
                                   (body.storage_id, adapter.organization_id, body.parent_id, name)).fetchone()
        except errors.UniqueViolation:
            raise HTTPException(409, 'Já existe uma pasta com esse nome aqui.')
        audit(conn, user, 'folder.create', str(row['id']), {'name': name})
    return row


@router.patch('/folders/{folder_id}')
def update_folder(folder_id: UUID, body: FolderUpdate, user=Depends(storage_manager)):
    """Rename a folder or move it under another folder of the same storage."""
    with db.connection() as conn:
        folder = visible_folder(conn, user, folder_id)
        name = folder_name(body.name) if body.name is not None else folder['name']
        parent = folder['parent_id']
        if 'parent_id' in body.model_fields_set:
            parent = body.parent_id
            if parent:
                visible_folder(conn, user, parent, folder['storage_id'])
                inside = conn.execute('''WITH RECURSIVE up AS (SELECT id,parent_id FROM setapi.folders WHERE id=%s
                    UNION ALL SELECT f.id,f.parent_id FROM setapi.folders f JOIN up ON f.id=up.parent_id) SELECT 1 FROM up WHERE id=%s''', (parent, folder_id)).fetchone()
                if inside:
                    raise HTTPException(422, 'Uma pasta não pode ir para dentro dela mesma.')
        try:
            with conn.transaction():
                row = conn.execute('UPDATE setapi.folders SET name=%s,parent_id=%s WHERE id=%s RETURNING ' + FOLDER_FIELDS, (name, parent, folder_id)).fetchone()
        except errors.UniqueViolation:
            raise HTTPException(409, 'Já existe uma pasta com esse nome aqui.')
        audit(conn, user, 'folder.update', str(folder_id), {'name': name, 'parent_id': str(parent) if parent else None})
    return row


@router.delete('/folders/{folder_id}', status_code=204)
def delete_folder(folder_id: UUID, recursive: bool = Query(False, description='Also delete every subfolder and file inside, from the provider too.'), user=Depends(storage_manager)):
    with db.connection() as conn:
        visible_folder(conn, user, folder_id)
        tree = [r['id'] for r in conn.execute('''WITH RECURSIVE down AS (SELECT id,0 AS depth FROM setapi.folders WHERE id=%s
            UNION ALL SELECT f.id,down.depth+1 FROM setapi.folders f JOIN down ON f.parent_id=down.id) SELECT id FROM down ORDER BY depth DESC''', (folder_id,)).fetchall()]
        files = conn.execute('SELECT id,storage_id,object_key FROM setapi.files WHERE folder_id=ANY(%s)', (tree,)).fetchall()
        if (len(tree) > 1 or files) and not recursive:
            raise HTTPException(409, 'A pasta não está vazia. Envie recursive=true para apagar tudo o que há dentro.')
    # Each file leaves the provider and the database together, so a failure never leaves a record without its object.
    for item in files:
        with db.connection() as conn:
            try:
                storage.get(conn, item['storage_id']).delete(item['object_key'])
            except Exception:
                raise HTTPException(502, 'O provedor não apagou um dos arquivos; nada mais foi removido. Tente de novo.')
            conn.execute('DELETE FROM setapi.files WHERE id=%s', (item['id'],))
    with db.connection() as conn:
        for fid in tree:
            conn.execute('DELETE FROM setapi.folders WHERE id=%s', (fid,))
        audit(conn, user, 'folder.delete', str(folder_id), {'folders': len(tree), 'files': len(files)})


class FileUpdate(BaseModel):
    name: str | None = Field(None, max_length=255)
    folder_id: UUID | None = Field(None, description='Destination folder in the same storage; send null to move to the top level.')


@router.patch('/files/{file_id}')
def update_file(file_id: UUID, body: FileUpdate, user=Depends(storage_manager)):
    """Rename a file or move it to another folder of the same storage."""
    where, params = visible_files(user)
    with db.connection() as conn:
        row = conn.execute('SELECT * FROM setapi.files WHERE id=%s AND ' + where, (file_id, *params)).fetchone()
        if not row:
            raise HTTPException(404, 'File not found')
        name = folder_name(body.name) if body.name is not None else row['name']
        folder = row['folder_id']
        if 'folder_id' in body.model_fields_set:
            folder = body.folder_id
            if folder:
                visible_folder(conn, user, folder, row['storage_id'])
        changed = conn.execute('UPDATE setapi.files SET name=%s,folder_id=%s WHERE id=%s RETURNING id,storage_id,folder_id,name,size,content_type,created_at',
                               (name, folder, file_id)).fetchone()
        audit(conn, user, 'file.update', str(file_id), {'name': name, 'folder_id': str(folder) if folder else None})
    return changed


@router.post('/files/{storage_id}', status_code=201)
def upload_file(storage_id: UUID, file: UploadFile, folder_id: UUID | None = Query(None, description='Folder of this storage to put the file in.'),
                user=Depends(storage_manager)):
    # Panel users and organization administrators upload, only into their organization's storage; app tokens never do.
    with db.connection() as conn:
        adapter = storage.get(conn, storage_id, storage.scope(user))
        if folder_id:
            visible_folder(conn, user, folder_id, storage_id)
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
            row = conn.execute('''INSERT INTO setapi.files(id,storage_id,owner_id,name,object_key,content_type,size,organization_id,folder_id)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id,name,size,folder_id''',
                (file_id, storage_id, user['id'], Path(file.filename or 'file').name[:255], key,
                 file.content_type or 'application/octet-stream', size, adapter.organization_id, folder_id)).fetchone()
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


def backup_scope(conn, user, organization_id):
    """Whose backups: the global administrator picks (None = platform); an organization administrator always gets their own."""
    return organization_id if is_admin(user) else tables.context(conn, user, organization_id)


def backup_storage(conn, storage_id, organization_id):
    # The platform backup is the whole database: only platform storage receives it.
    # An organization backup holds only its tables and goes to that organization's storage.
    if organization_id is not None and not conn.execute('SELECT 1 FROM setapi.organizations WHERE id=%s AND active', (organization_id,)).fetchone():
        raise HTTPException(422, 'Choose an active organization')
    return storage.get(conn, storage_id, organization_id)


@router.post('/backups', status_code=202)
def queue_backup(body: BackupCreate, user=Depends(builder)):
    with db.connection() as conn:
        body.organization_id = backup_scope(conn, user, body.organization_id)
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
                 all: bool = Query(False, description='Every backup: the platform and all organizations.'), user=Depends(builder)):
    with db.connection() as conn:
        organization_id, all = backup_scope(conn, user, organization_id), all and is_admin(user)
        return {'data': conn.execute('SELECT * FROM setapi.backups WHERE %s OR organization_id IS NOT DISTINCT FROM %s ORDER BY created_at DESC LIMIT 200',
                                     (all, organization_id)).fetchall()}


class ScheduleCreate(BaseModel):
    storage_id: UUID
    every_hours: int = Field(default=24, ge=1, le=8760)
    retention: int = Field(default=7, ge=1, le=7, description='How many completed copies to keep, from 1 to 7.')
    organization_id: UUID | None = BACKUP_ORG


@router.post('/backup-schedules', status_code=201)
def create_schedule(body: ScheduleCreate, user=Depends(builder)):
    with db.connection() as conn:
        body.organization_id = backup_scope(conn, user, body.organization_id)
        backup_storage(conn, body.storage_id, body.organization_id)
        row = conn.execute('INSERT INTO setapi.schedules(storage_id,every_hours,retention,organization_id) VALUES(%s,%s,%s,%s) RETURNING *',
            (body.storage_id, body.every_hours, body.retention, body.organization_id)).fetchone()
        audit(conn, user, 'backup.schedule', str(row['id']))
    return row


@router.get('/backup-schedules')
def list_schedules(organization_id: UUID | None = Query(None, description='Schedules of this organization; omit for the platform schedules.'),
                   all: bool = Query(False, description='Every schedule: the platform and all organizations.'), user=Depends(builder)):
    with db.connection() as conn:
        organization_id, all = backup_scope(conn, user, organization_id), all and is_admin(user)
        return {'data': conn.execute('SELECT * FROM setapi.schedules WHERE %s OR organization_id IS NOT DISTINCT FROM %s ORDER BY next_run',
                                     (all, organization_id)).fetchall()}


@router.delete('/backup-schedules/{schedule_id}', status_code=204)
def delete_schedule(schedule_id: UUID, user=Depends(builder)):
    with db.connection() as conn:
        owner = conn.execute('SELECT organization_id FROM setapi.schedules WHERE id=%s', (schedule_id,)).fetchone()
        if not owner or (not is_admin(user) and owner['organization_id'] != user.get('tenant_id')):
            raise HTTPException(404, 'Schedule not found')
        conn.execute('DELETE FROM setapi.schedules WHERE id=%s', (schedule_id,))
        audit(conn, user, 'backup.schedule.delete', str(schedule_id))


@router.post('/backups/{backup_id}/restore', status_code=202)
def queue_restore(backup_id: UUID, user=Depends(builder)):
    """Bring the organization's tables back to this backup. The current state is saved first as a backup."""
    with db.connection() as conn:
        backup = conn.execute("SELECT * FROM setapi.backups WHERE id=%s AND status='completed'", (backup_id,)).fetchone()
        if not backup or backup['organization_id'] is None or backup_scope(conn, user, backup['organization_id']) != backup['organization_id']:
            raise HTTPException(404, 'Restore point not found')
        if conn.execute("SELECT 1 FROM setapi.restores WHERE organization_id=%s AND status IN ('queued','running')", (backup['organization_id'],)).fetchone():
            raise HTTPException(409, 'Já existe uma restauração em andamento para esta organização.')
        row = conn.execute('INSERT INTO setapi.restores(organization_id,backup_id,requested_by) VALUES(%s,%s,%s) RETURNING *',
                           (backup['organization_id'], backup_id, user['id'])).fetchone()
        audit(conn, user, 'backup.restore', str(backup_id), {'organization_id': str(backup['organization_id'])})
    return row


@router.get('/restores')
def list_restores(organization_id: UUID | None = Query(None, description='Restores of this organization.'), user=Depends(builder)):
    with db.connection() as conn:
        organization_id = backup_scope(conn, user, organization_id)
        return {'data': conn.execute('SELECT * FROM setapi.restores WHERE organization_id=%s ORDER BY created_at DESC LIMIT 50', (organization_id,)).fetchall()}


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
