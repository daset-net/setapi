from datetime import datetime, timezone
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, ConfigDict
from psycopg import sql
from psycopg.types.json import Jsonb
from argon2.exceptions import VerificationError
from . import db, tables, policies
from .organizations import require_active
from .config import settings
from .security import admin, principal, is_admin, allowed, audit, issue, validate_scopes, passwords, DUMMY_HASH, verify_password, hash_password, rate_limit

router = APIRouter(prefix='/api', tags=['Administration'])


class Login(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=1024)
    otp: str = Field(default='', max_length=100)


@router.post('/auth/login')
def login(body: Login, request: Request, response: Response):
    from .accounts import login_user
    user, token = login_user(body, request, 'panel')
    response.set_cookie('setapi_session', token['token'], httponly=True, secure=settings().cookie_secure, samesite='lax', max_age=43200)
    response.headers['Cache-Control'] = 'no-store'
    return {'user': {'id': user['id'], 'email': user['email'], 'role': user['role']}, 'expires_at': token['expires_at']}


@router.get('/auth/me')
def me(user=Depends(principal)):
    return {'id': user['id'], 'email': user['email'], 'admin': is_admin(user), 'scopes': user['scopes'], 'mfa_enabled':user['mfa_enabled'], 'audience':user['audience'], 'organization_id':user['tenant_id']}


@router.post('/auth/logout', status_code=204)
def logout(response: Response, user=Depends(principal)):
    with db.connection() as conn:
        conn.execute('UPDATE setapi.tokens SET revoked_at=now() WHERE id=%s', (user['token_id'],))
    response.delete_cookie('setapi_session')


@router.get('/status')
def status(user=Depends(admin)):
    with db.connection() as conn:
        version = conn.execute('SHOW server_version').fetchone()['server_version']
        counts = conn.execute('''SELECT (SELECT count(*) FROM setapi.users) AS users,
          (SELECT count(*) FROM setapi.tokens WHERE revoked_at IS NULL AND expires_at>now()) AS tokens,
          (SELECT count(*) FROM setapi.outbox) AS pending_events,
          (SELECT count(*) FROM setapi.backups WHERE status='queued') AS queued_backups''').fetchone()
    from . import postgrest
    return {'read_engine': 'postgrest' if postgrest.enabled() else 'native', 'postgres': version, 'redis': db.cache.ping(), 'worker_alive': bool(db.cache.get('setapi:worker:heartbeat')), 'db_pool':db.pool.get_stats(), **counts}


@router.get('/tables')
def list_tables(user=Depends(principal)):
    with db.connection() as conn:
        rows = conn.execute("SELECT table_name AS name FROM information_schema.tables WHERE table_schema='data' AND table_type='BASE TABLE' ORDER BY table_name").fetchall()
        result=[]
        for r in rows:
            if not allowed(user,r['name'],'read'):continue
            try:
                readable=policies.fields(conn,r['name'],user,'read')
            except HTTPException as exc:
                if exc.status_code==403:continue
                raise
            writable=policies.fields(conn,r['name'],user,'write')
            result.append(dict(r,columns=[dict(c,default_value=None,writable=c['name'] in writable and c['name'] not in tables.RESERVED) for c in tables.columns(conn,r['name']) if c['name'] in readable]))
        return {'data':result}


@router.post('/tables', status_code=201)
def create_table(body: tables.Table, user=Depends(admin)):
    tables.identifier(body.name)
    if len({c.name for c in body.columns}) != len(body.columns):
        raise HTTPException(422, 'Duplicate columns')
    definitions = [sql.SQL('id uuid PRIMARY KEY DEFAULT gen_random_uuid()'),
                   sql.SQL('created_at timestamptz NOT NULL DEFAULT now()'),
                   sql.SQL('updated_at timestamptz NOT NULL DEFAULT now()')]
    if body.organization_isolated:
        if any(c.name=='organization_id' for c in body.columns):raise HTTPException(422,'organization_id is created automatically')
        definitions.append(sql.SQL('organization_id uuid REFERENCES setapi.organizations(id) ON DELETE RESTRICT'))
    definitions.extend(tables.column_sql(c) for c in body.columns)
    with db.connection() as conn:
        conn.execute(sql.SQL('CREATE TABLE data.{} ({})').format(sql.Identifier(body.name), sql.SQL(',').join(definitions)))
        if body.organization_isolated:
            readable=['id','created_at','updated_at']+[c.name for c in body.columns]
            writable=[c.name for c in body.columns]
            conn.execute("INSERT INTO setapi.policies VALUES(%s,NULL,'organization_id',%s,%s)",(body.name,Jsonb(readable),Jsonb(writable)))
            index='so_'+__import__('hashlib').sha256(body.name.encode()).hexdigest()[:20]
            conn.execute(sql.SQL('CREATE INDEX {} ON data.{} (organization_id,created_at,id)').format(sql.Identifier(index),sql.Identifier(body.name)))
        tables.manage(conn,body.name)
        audit(conn, user, 'table.create', body.name, body.model_dump())
        tables.event(conn, body.name, 'schema', body.name)
    return {'name': body.name, 'endpoint': '/api/data/' + body.name}


@router.post('/tables/{table}/columns', status_code=201)
def add_column(table: str, body: tables.Column, user=Depends(admin)):
    definition = tables.column_sql(body)
    with db.connection() as conn:
        tables.exists(conn, table)
        conn.execute(sql.SQL('ALTER TABLE data.{} ADD COLUMN {}').format(sql.Identifier(table), definition))
        audit(conn, user, 'column.create', table, body.model_dump())
        tables.event(conn, table, 'schema', table)
    return {'ok': True}


class Rename(BaseModel):
    name: str


@router.patch('/tables/{table}/columns/{column}')
def rename_column(table: str, column: str, body: Rename, user=Depends(admin)):
    tables.identifier(column)
    tables.identifier(body.name)
    if column in tables.RESERVED or body.name in tables.RESERVED:
        raise HTTPException(422, 'System columns cannot be renamed')
    with db.connection() as conn:
        tables.exists(conn, table)
        policies.protect_column(conn,table,column)
        conn.execute(sql.SQL('ALTER TABLE data.{} RENAME COLUMN {} TO {}').format(sql.Identifier(table), sql.Identifier(column), sql.Identifier(body.name)))
        audit(conn, user, 'column.rename', table, {'old': column, 'new': body.name})
        tables.event(conn, table, 'schema', table)
    return {'ok': True}


@router.delete('/tables/{table}/columns/{column}', status_code=204)
def drop_column(table: str, column: str, confirm: str, user=Depends(admin)):
    tables.identifier(column)
    if column in tables.RESERVED or confirm != f'{table}.{column}':
        raise HTTPException(422, 'Confirm the exact table.column; system columns cannot be deleted')
    with db.connection() as conn:
        tables.exists(conn, table)
        policies.protect_column(conn,table,column)
        conn.execute(sql.SQL('ALTER TABLE data.{} DROP COLUMN {} RESTRICT').format(sql.Identifier(table), sql.Identifier(column)))
        audit(conn, user, 'column.delete', table, {'column': column})
        tables.event(conn, table, 'schema', table)


@router.delete('/tables/{table}', status_code=204)
def drop_table(table: str, confirm: str, user=Depends(admin)):
    if confirm != table:
        raise HTTPException(422, 'Confirm the exact table name')
    with db.connection() as conn:
        tables.exists(conn, table)
        conn.execute(sql.SQL('DROP TABLE data.{} RESTRICT').format(sql.Identifier(table)))
        # A reused table name must not silently inherit an old token's access.
        conn.execute('DELETE FROM setapi.policies WHERE table_name=%s',(table,))
        conn.execute('UPDATE setapi.tokens SET scopes=scopes-%s', (table,))
        conn.execute('UPDATE setapi.users SET scopes=scopes-%s', (table,))
        audit(conn, user, 'table.delete', table)


class UserCreate(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=12, max_length=1024)
    role: str = 'member'
    scopes: dict[str, list[str]] = Field(default_factory=dict)
    audience: str = 'panel'
    tenant_id: UUID | None = None


@router.get('/users')
def users(user=Depends(admin)):
    with db.connection() as conn:
        return {'data': conn.execute('SELECT id,email,role,active,scopes,audience,tenant_id,(mfa_secret IS NOT NULL) AS mfa_enabled,created_at FROM setapi.users ORDER BY created_at').fetchall()}


@router.post('/users', status_code=201)
def create_user(body: UserCreate, user=Depends(admin)):
    import re
    if body.role not in ('admin', 'member') or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',body.email):
        raise HTTPException(422, 'Invalid role or email')
    if body.role=='admin' and body.tenant_id is not None:
        raise HTTPException(422,'Global administrators cannot be organization members')
    if body.audience not in ('panel','app') or (body.audience == 'app' and body.role != 'member'):
        raise HTTPException(422, 'App users must be members')
    validate_scopes(body.scopes)
    encoded = hash_password(body.password)
    with db.connection() as conn:
        require_active(conn,body.tenant_id)
        row = conn.execute('INSERT INTO setapi.users(email,password_hash,role,scopes,audience,tenant_id) VALUES(%s,%s,%s,%s,%s,%s) RETURNING id,email,role',
            (body.email.lower(), encoded, body.role, Jsonb(body.scopes), body.audience, body.tenant_id)).fetchone()
        audit(conn, user, 'user.create', str(row['id']))
    return row


class Active(BaseModel):
    active: bool


@router.patch('/users/{user_id}')
def toggle_user(user_id: UUID, body: Active, user=Depends(admin)):
    if user_id == user['id']:
        raise HTTPException(409, 'Cannot deactivate your own account')
    with db.connection() as conn:
        row = conn.execute('UPDATE setapi.users SET active=%s WHERE id=%s RETURNING id,active', (body.active, user_id)).fetchone()
        if not row:
            raise HTTPException(404, 'User not found')
        if not body.active:
            conn.execute('DELETE FROM setapi.action_tokens WHERE user_id=%s',(user_id,))
            conn.execute('UPDATE setapi.tokens SET revoked_at=now() WHERE user_id=%s', (user_id,))
        audit(conn, user, 'user.active', str(user_id), {'active': body.active})
    return row


class TokenCreate(BaseModel):
    user_id: UUID | None = None
    name: str = Field(min_length=1, max_length=100)
    hours: int = Field(default=720, ge=1, le=8760)
    admin: bool = False
    scopes: dict[str, list[str]] = Field(default_factory=dict)


@router.get('/tokens')
def tokens(user=Depends(admin)):
    with db.connection() as conn:
        return {'data': conn.execute("SELECT t.id,t.name,t.prefix,t.scopes,t.admin,t.expires_at,t.revoked_at,t.user_id,u.tenant_id AS organization_id FROM setapi.tokens t JOIN setapi.users u ON u.id=t.user_id WHERE t.kind='api' ORDER BY t.created_at DESC LIMIT 200").fetchall()}


@router.post('/tokens', status_code=201)
def create_token(body: TokenCreate, response: Response, user=Depends(admin)):
    validate_scopes(body.scopes)
    with db.connection() as conn:
        owner_id=body.user_id or user['id']
        owner=conn.execute('SELECT id,role,active,tenant_id,scopes FROM setapi.users WHERE id=%s',(owner_id,)).fetchone()
        if not owner or not owner['active']:raise HTTPException(422,'Choose an active token owner')
        require_active(conn,owner['tenant_id'])
        if body.admin and (owner['role']!='admin' or owner['tenant_id'] is not None):
            raise HTTPException(422,'Organization tokens cannot have global administrative access')
        if owner['role']!='admin' and any(not set(actions)<=set(owner['scopes'].get(table,[])) for table,actions in body.scopes.items()):
            raise HTTPException(422,'Token permissions must be within the owner permissions')
        result = issue(conn, owner_id, body.name, scopes=body.scopes, admin=body.admin, hours=body.hours)
        audit(conn, user, 'token.create', str(result['id']), {'name': body.name, 'admin': body.admin, 'scopes': body.scopes})
    response.headers['Cache-Control'] = 'no-store'
    return result


@router.delete('/tokens/{token_id}', status_code=204)
def revoke_token(token_id: UUID, user=Depends(admin)):
    with db.connection() as conn:
        conn.execute('UPDATE setapi.tokens SET revoked_at=now() WHERE id=%s', (token_id,))
        audit(conn, user, 'token.revoke', str(token_id))


@router.get('/audit')
def audit_log(user=Depends(admin)):
    with db.connection() as conn:
        return {'data': conn.execute('SELECT * FROM setapi.audit ORDER BY id DESC LIMIT 100').fetchall()}


class IndexCreate(BaseModel):
    columns:list[str]=Field(min_length=1,max_length=4)
    unique:bool=False


@router.get('/tables/{table}/indexes')
def indexes(table:str,user=Depends(admin)):
    with db.connection() as conn:
        tables.exists(conn,table)
        return {'data':conn.execute("SELECT indexname,indexdef FROM pg_indexes WHERE schemaname='data' AND tablename=%s",(table,)).fetchall()}


@router.post('/tables/{table}/indexes',status_code=201)
def add_index(table:str,body:IndexCreate,user=Depends(admin)):
    import hashlib
    with db.connection() as conn:
        cols={c['name'] for c in tables.columns(conn,table)}
        if not set(body.columns)<=cols or len(set(body.columns))!=len(body.columns):raise HTTPException(422,'Unknown or duplicate field')
        name='si_'+hashlib.sha256((table+str(body.columns)+str(body.unique)).encode()).hexdigest()[:20]
        conn.execute(sql.SQL('CREATE {} INDEX IF NOT EXISTS {} ON data.{} ({})').format(sql.SQL('UNIQUE' if body.unique else ''),sql.Identifier(name),sql.Identifier(table),sql.SQL(',').join(map(sql.Identifier,body.columns))))
        audit(conn,user,'index.create',table)
    return {'name':name}


@router.delete('/tables/{table}/indexes/{index}',status_code=204)
def drop_index(table:str,index:str,user=Depends(admin)):
    with db.connection() as conn:
        if not index.startswith('si_') or not conn.execute("SELECT 1 FROM pg_indexes WHERE schemaname='data' AND tablename=%s AND indexname=%s",(table,index)).fetchone():
            raise HTTPException(422,'Only user-created SETAPI indexes can be removed')
        conn.execute(sql.SQL('DROP INDEX data.{}').format(sql.Identifier(index)))
        audit(conn,user,'index.delete',table)


class ColumnEdit(BaseModel):
    type:str
    nullable:bool=True
    confirm:str


@router.put('/tables/{table}/columns/{column}')
def edit_column(table:str,column:str,body:ColumnEdit,user=Depends(admin)):
    if column in tables.RESERVED or body.type not in tables.TYPES or body.confirm!=table+'.'+column:
        raise HTTPException(422,'Confirm the exact table.column and a supported type')
    with db.connection() as conn:
        cols={c['name'] for c in tables.columns(conn,table)}
        if column not in cols:raise HTTPException(404,'Field not found')
        policies.protect_column(conn,table,column)
        conn.execute(sql.SQL('ALTER TABLE data.{} ALTER COLUMN {} TYPE {} USING {}::{}, ALTER COLUMN {} {} NOT NULL').format(sql.Identifier(table),sql.Identifier(column),sql.SQL(tables.TYPES[body.type]),sql.Identifier(column),sql.SQL(tables.TYPES[body.type]),sql.Identifier(column),sql.SQL('DROP' if body.nullable else 'SET')))
        audit(conn,user,'column.type',table,{'column':column,'type':body.type})
    return {'ok':True}


class Adopt(BaseModel):
    confirm:str


@router.post('/tables/{table}/adopt')
def adopt_table(table:str,body:Adopt,user=Depends(admin)):
    if body.confirm!=table:raise HTTPException(422,'Confirm the exact table name')
    with db.connection() as conn:
        tables.exists(conn,table)
        cols={c['name']:c['type'] for c in tables.columns(conn,table)}
        if 'id' in cols and cols['id']!='uuid':raise HTTPException(422,'Existing id must be UUID; migrate its relationships before adoption')
        if 'id' not in cols:
            conn.execute(sql.SQL('ALTER TABLE data.{} ADD COLUMN id uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE').format(sql.Identifier(table)))
        for col in ('created_at','updated_at'):
            if col not in cols:
                conn.execute(sql.SQL('ALTER TABLE data.{} ADD COLUMN {} timestamptz NOT NULL DEFAULT now()').format(sql.Identifier(table),sql.Identifier(col)))
        conn.execute(sql.SQL('ALTER TABLE data.{} ALTER COLUMN id SET NOT NULL').format(sql.Identifier(table)))
        unique_name='su_'+__import__('hashlib').sha256(table.encode()).hexdigest()[:20]
        conn.execute(sql.SQL('CREATE UNIQUE INDEX IF NOT EXISTS {} ON data.{} (id)').format(sql.Identifier(unique_name),sql.Identifier(table)))
        tables.manage(conn,table)
        audit(conn,user,'table.adopt',table)
    return {'ok':True}
