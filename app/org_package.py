"""Move an organization between SETAPI servers: a package protected by a passphrase, not by the server key.

The package holds the organization's tables and records, field policies, accounts (with their password hashes and
two-step secrets), storage connections with their credentials, folders, the file list and backup schedules.
File contents stay in the providers; the new server reaches them through the same connections.
Every id is kept, so records, files and accounts keep pointing at each other. Import refuses anything that
already exists on the target server.
"""
import base64
import json
import os
import re
import secrets
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from psycopg import sql
from psycopg.types.json import Jsonb
from starlette.background import BackgroundTask
from . import db, org_restore, storage, tables
from .backup import decrypt_file, encrypt_file
from .config import settings
from .security import admin, audit

router = APIRouter(prefix='/api/organizations', tags=['Organizations'])
MAGIC = b'SETAPIORG1'
FORMAT = 1
PASSPHRASE = 'At least 12 characters. Without it the package cannot be opened; SETAPI does not keep it.'


def derive(passphrase, salt):
    key = Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1).derive(passphrase.encode())
    return base64.urlsafe_b64encode(key).decode()


def check_passphrase(value):
    if len(value or '') < 12:
        raise HTTPException(422, 'Use uma senha do pacote com pelo menos 12 caracteres.')
    return value


def rows(conn, query, params):
    return [dict(r) for r in conn.execute(query, params).fetchall()]


def snapshot(conn, organization_id):
    org = conn.execute('SELECT id,name,active,created_at,table_prefix FROM setapi.organizations WHERE id=%s', (organization_id,)).fetchone()
    if not org:
        raise HTTPException(404, 'Organization not found')
    users = rows(conn, 'SELECT * FROM setapi.users WHERE tenant_id=%s ORDER BY created_at', (organization_id,))
    for user in users:
        # Two-step secrets are encrypted with this server's key: carry them in the clear, inside the protected package.
        user['mfa_secret'] = storage.decrypt(user['mfa_secret']) if user['mfa_secret'] else None
    stores = rows(conn, 'SELECT * FROM setapi.storages WHERE organization_id=%s ORDER BY created_at', (organization_id,))
    for item in stores:
        item['config'] = storage.decrypt(item.pop('config_encrypted'))
    return {
        'format': FORMAT, 'exported_at': datetime.now(timezone.utc).isoformat(), 'organization': dict(org), 'users': users,
        'storages': stores,
        'folders': rows(conn, 'SELECT * FROM setapi.folders WHERE organization_id=%s ORDER BY created_at', (organization_id,)),
        'files': rows(conn, 'SELECT * FROM setapi.files WHERE organization_id=%s ORDER BY created_at', (organization_id,)),
        'schedules': rows(conn, 'SELECT * FROM setapi.schedules WHERE organization_id=%s', (organization_id,)),
        'policies': rows(conn, 'SELECT * FROM setapi.policies WHERE starts_with(table_name,%s)', (org['table_prefix'],)),
    }


@router.post('/{organization_id}/package')
def export_package(organization_id: UUID, passphrase: str = Form(description=PASSPHRASE), user=Depends(admin)):
    """Download the organization as a package to import on another SETAPI server."""
    check_passphrase(passphrase)
    folder = Path(tempfile.mkdtemp(prefix='setapi-package-'))
    try:
        with db.connection() as conn:
            manifest = snapshot(conn, organization_id)
            audit(conn, user, 'organization.export', str(organization_id))
        prefix = manifest['organization']['table_prefix']
        result = subprocess.run(['pg_dump', '--format=custom', '--no-owner', '--no-acl', '--table=data.' + prefix + '*', '--file', str(folder / 'database.dump')],
                                env=db.pg_environment(settings().database_url), capture_output=True, timeout=1800)
        if result.returncode:
            raise HTTPException(500, 'pg_dump falhou ao exportar as tabelas da organização.')
        (folder / 'organization.json').write_text(json.dumps(manifest, default=str, ensure_ascii=False))
        with tarfile.open(folder / 'bundle.tar', 'w') as archive:
            for name in ('organization.json', 'database.dump'):
                archive.add(folder / name, arcname=name)
        salt = secrets.token_bytes(16)
        encrypt_file(folder / 'bundle.tar', folder / 'sealed', derive(passphrase, salt))
        package = folder / 'package'
        with open(package, 'wb') as out, open(folder / 'sealed', 'rb') as sealed:
            out.write(MAGIC + salt)
            while chunk := sealed.read(1024 * 1024):
                out.write(chunk)
        for name in ('organization.json', 'database.dump', 'bundle.tar', 'sealed'):
            (folder / name).unlink(missing_ok=True)
    except BaseException:
        __import__('shutil').rmtree(folder, ignore_errors=True)
        raise
    slug = re.sub(r'[^a-z0-9]+', '-', __import__('unicodedata').normalize('NFKD', manifest['organization']['name']).encode('ascii', 'ignore').decode().lower()).strip('-') or 'organizacao'
    return FileResponse(package, filename=f'{slug}-{datetime.now().strftime("%Y-%m-%d")}.setapi-org', media_type='application/octet-stream',
                        background=BackgroundTask(__import__('shutil').rmtree, folder, True), headers={'Cache-Control': 'no-store'})


def open_package(upload, passphrase, folder):
    raw = folder / 'package'
    with open(raw, 'wb') as out:
        while chunk := upload.file.read(1024 * 1024):
            out.write(chunk)
    with open(raw, 'rb') as src:
        if src.read(len(MAGIC)) != MAGIC:
            raise HTTPException(422, 'Este arquivo não é um pacote de organização do SETAPI.')
        salt = src.read(16)
        with open(folder / 'sealed', 'wb') as out:
            while chunk := src.read(1024 * 1024):
                out.write(chunk)
    try:
        decrypt_file(folder / 'sealed', folder / 'bundle.tar', derive(passphrase, salt))
    except Exception:
        raise HTTPException(422, 'Senha do pacote incorreta, ou o arquivo está corrompido.')
    with tarfile.open(folder / 'bundle.tar') as archive:
        members = [m for m in archive.getmembers() if m.name in ('organization.json', 'database.dump') and m.isfile()]
        archive.extractall(folder, members=members, filter='data')
    manifest = json.loads((folder / 'organization.json').read_text())
    if manifest.get('format') != FORMAT:
        raise HTTPException(422, 'Versão de pacote não suportada por este servidor.')
    if not tables.PREFIXED.fullmatch(manifest['organization']['table_prefix']):
        raise HTTPException(422, 'Pacote inválido.')
    return manifest


def conflicts(conn, manifest):
    """Everything the package would create must be free on this server."""
    org, found = manifest['organization'], []
    checks = [
        ('a organização', 'SELECT 1 FROM setapi.organizations WHERE id=%s OR table_prefix=%s', (org['id'], org['table_prefix'])),
        ('tabelas com o mesmo prefixo', "SELECT 1 FROM information_schema.tables WHERE table_schema='data' AND starts_with(table_name,%s)", (org['table_prefix'],)),
        ('usuários', 'SELECT 1 FROM setapi.users WHERE id=ANY(%s) OR lower(email)=ANY(%s)',
         ([u['id'] for u in manifest['users']], [u['email'].lower() for u in manifest['users']])),
        ('conexões de storage', 'SELECT 1 FROM setapi.storages WHERE id=ANY(%s)', ([s['id'] for s in manifest['storages']],)),
        ('pastas', 'SELECT 1 FROM setapi.folders WHERE id=ANY(%s)', ([f['id'] for f in manifest['folders']],)),
        ('arquivos', 'SELECT 1 FROM setapi.files WHERE id=ANY(%s)', ([f['id'] for f in manifest['files']],)),
    ]
    for label, query, params in checks:
        if conn.execute(query, params).fetchone():
            found.append(label)
    return found


def insert(conn, table, row, json_fields=()):
    names = list(row)
    values = [Jsonb(row[n]) if n in json_fields else row[n] for n in names]
    conn.execute(sql.SQL('INSERT INTO setapi.{} ({}) VALUES ({})').format(
        sql.Identifier(table), sql.SQL(',').join(map(sql.Identifier, names)), sql.SQL(',').join(sql.Placeholder() * len(names))), values)


def parents_first(folders):
    ordered, placed, pending = [], set(), list(folders)
    while pending:
        ready = [f for f in pending if not f['parent_id'] or f['parent_id'] in placed]
        if not ready:
            raise HTTPException(422, 'Pacote inválido: pastas sem a pasta de cima.')
        for f in ready:
            ordered.append(f)
            placed.add(f['id'])
        pending = [f for f in pending if f['id'] not in placed]
    return ordered


@router.post('/import', status_code=201)
def import_package(file: UploadFile = File(description='Package exported by another SETAPI server (.setapi-org).'),
                   passphrase: str = Form(description='The passphrase chosen when the package was exported.'), user=Depends(admin)):
    """Create an organization from a package exported by another SETAPI server, keeping every id."""
    folder = Path(tempfile.mkdtemp(prefix='setapi-import-'))
    try:
        manifest = open_package(file, passphrase, folder)
        org, prefix = manifest['organization'], manifest['organization']['table_prefix']
        with db.connection() as conn:
            taken = conflicts(conn, manifest)
        if taken:
            raise HTTPException(409, 'Já existem neste servidor: ' + ', '.join(taken) + '. Nada foi importado.')
        restored = set()
        if (folder / 'database.dump').stat().st_size:
            try:
                restored = org_restore.plan(folder / 'database.dump', prefix, folder)
            except org_restore.RestoreError as exc:
                raise HTTPException(422, str(exc))
            script = subprocess.run(['pg_restore', '--use-list', str(folder / 'restore.list'), '--no-owner', '--no-acl', '--file', str(folder / 'restore.sql'), str(folder / 'database.dump')],
                                    capture_output=True, timeout=600)
            if script.returncode:
                raise HTTPException(422, 'Não foi possível ler as tabelas do pacote.')
            run = subprocess.run(['psql', '--no-psqlrc', '--quiet', '--single-transaction', '-v', 'ON_ERROR_STOP=1', '-f', str(folder / 'restore.sql')],
                                 env=db.pg_environment(settings().database_url), capture_output=True, timeout=1800)
            if run.returncode:
                raise HTTPException(500, 'As tabelas do pacote não puderam ser criadas. Nada foi importado.')
        try:
            with db.connection() as conn:
                insert(conn, 'organizations', {k: org[k] for k in ('id', 'name', 'active', 'created_at', 'table_prefix')})
                for u in manifest['users']:
                    u = dict(u, mfa_secret=storage.encrypt(u['mfa_secret']) if u['mfa_secret'] else None)
                    insert(conn, 'users', u, ('scopes', 'recovery_hashes'))
                for s in manifest['storages']:
                    config = s.pop('config')
                    storage.validate(s['provider'], config)
                    insert(conn, 'storages', dict(s, config_encrypted=storage.encrypt(config)))
                for f in parents_first(manifest['folders']):
                    insert(conn, 'folders', f)
                members = {u['id'] for u in manifest['users']}
                for f in manifest['files']:
                    # Files sent by a global administrator of the old server now belong to whoever imports.
                    insert(conn, 'files', dict(f, owner_id=f['owner_id'] if f['owner_id'] in members else user['id']))
                for s in manifest['schedules']:
                    insert(conn, 'schedules', s)
                for p in manifest['policies']:
                    if p['table_name'] in restored:
                        insert(conn, 'policies', p, ('read_fields', 'write_fields'))
                for name in sorted(restored):
                    tables.manage(conn, name)
                    tables.event(conn, name, 'schema', name)
                conn.execute("NOTIFY pgrst, 'reload schema'")
                audit(conn, user, 'organization.import', str(org['id']), {'name': org['name'], 'tables': len(restored), 'users': len(manifest['users'])})
        except BaseException:
            # The tables were committed by psql; without their organization they must go.
            with db.connection() as conn:
                for name in restored:
                    conn.execute(sql.SQL('DROP TABLE IF EXISTS data.{} CASCADE').format(sql.Identifier(name)))
            raise
        return {'id': org['id'], 'name': org['name'], 'tables': len(restored), 'users': len(manifest['users']),
                'storages': len(manifest['storages']), 'files': len(manifest['files'])}
    finally:
        __import__('shutil').rmtree(folder, ignore_errors=True)
        file.file.close()
