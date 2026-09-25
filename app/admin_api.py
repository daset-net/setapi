from datetime import datetime, timezone
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, ConfigDict
from psycopg import sql
from psycopg.types.json import Jsonb
from argon2.exceptions import VerificationError
from . import db, tables
from .config import settings
from .security import admin, principal, is_admin, allowed, audit, issue, validate_scopes, passwords, DUMMY_HASH

router = APIRouter(prefix='/api', tags=['Administration'])


class Login(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=1024)


@router.post('/auth/login')
def login(body: Login, request: Request, response: Response):
    import hashlib
    # IP limit plus account limit. Proxy headers are accepted only from trusted proxies configured in Uvicorn.
    keys = ['setapi:login:ip:' + request.client.host,
            'setapi:login:email:' + hashlib.sha256(body.email.lower().encode()).hexdigest()]
    for key in keys:
        count = db.cache.eval("local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],300) end; return n", 1, key)
        if count > 20:
            raise HTTPException(429, 'Too many attempts; retry in five minutes')
    with db.connection() as conn:
        user = conn.execute('SELECT * FROM setapi.users WHERE email=%s', (body.email.lower(),)).fetchone()
        try:
            valid = passwords.verify(user['password_hash'] if user else DUMMY_HASH, body.password)
        except VerificationError:
            valid = False
        if not valid or not user or not user['active']:
            raise HTTPException(401, 'Invalid credentials')
        token = issue(conn, user['id'], 'Panel session', 'session', user['scopes'], user['role'] == 'admin', 12)
        audit(conn, user, 'auth.login', 'session')
    response.set_cookie('setapi_session', token['token'], httponly=True, secure=settings().cookie_secure, samesite='strict', max_age=43200)
    response.headers['Cache-Control'] = 'no-store'
    return {'user': {'id': user['id'], 'email': user['email'], 'role': user['role']}, 'expires_at': token['expires_at']}


@router.get('/auth/me')
def me(user=Depends(principal)):
    return {'id': user['id'], 'email': user['email'], 'admin': is_admin(user), 'scopes': user['scopes']}


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
    return {'postgres': version, 'redis': db.cache.ping(), 'worker_alive': bool(db.cache.get('setapi:worker:heartbeat')), **counts}


@router.get('/tables')
def list_tables(user=Depends(principal)):
    with db.connection() as conn:
        rows = conn.execute("SELECT table_name AS name FROM information_schema.tables WHERE table_schema='data' AND table_type='BASE TABLE' ORDER BY table_name").fetchall()
        return {'data': [dict(r, columns=tables.columns(conn, r['name'])) for r in rows if allowed(user, r['name'], 'read')]}


@router.post('/tables', status_code=201)
def create_table(body: tables.Table, user=Depends(admin)):
    tables.identifier(body.name)
    if len({c.name for c in body.columns}) != len(body.columns):
        raise HTTPException(422, 'Duplicate columns')
    definitions = [sql.SQL('id uuid PRIMARY KEY DEFAULT gen_random_uuid()'),
                   sql.SQL('created_at timestamptz NOT NULL DEFAULT now()'),
                   sql.SQL('updated_at timestamptz NOT NULL DEFAULT now()')]
    definitions.extend(tables.column_sql(c) for c in body.columns)
    with db.connection() as conn:
        conn.execute(sql.SQL('CREATE TABLE data.{} ({})').format(sql.Identifier(body.name), sql.SQL(',').join(definitions)))
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
        conn.execute('UPDATE setapi.tokens SET scopes=scopes-%s', (table,))
        conn.execute('UPDATE setapi.users SET scopes=scopes-%s', (table,))
        audit(conn, user, 'table.delete', table)


class UserCreate(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=12, max_length=1024)
    role: str = 'member'
    scopes: dict[str, list[str]] = Field(default_factory=dict)


@router.get('/users')
def users(user=Depends(admin)):
    with db.connection() as conn:
        return {'data': conn.execute('SELECT id,email,role,active,scopes,created_at FROM setapi.users ORDER BY created_at').fetchall()}


@router.post('/users', status_code=201)
def create_user(body: UserCreate, user=Depends(admin)):
    if body.role not in ('admin', 'member') or '@' not in body.email:
        raise HTTPException(422, 'Invalid role or email')
    validate_scopes(body.scopes)
    with db.connection() as conn:
        row = conn.execute('INSERT INTO setapi.users(email,password_hash,role,scopes) VALUES(%s,%s,%s,%s) RETURNING id,email,role',
            (body.email.lower(), passwords.hash(body.password), body.role, Jsonb(body.scopes))).fetchone()
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
            conn.execute('UPDATE setapi.tokens SET revoked_at=now() WHERE user_id=%s', (user_id,))
        audit(conn, user, 'user.active', str(user_id), {'active': body.active})
    return row


class TokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    hours: int = Field(default=720, ge=1, le=8760)
    admin: bool = False
    scopes: dict[str, list[str]] = Field(default_factory=dict)


@router.get('/tokens')
def tokens(user=Depends(admin)):
    with db.connection() as conn:
        return {'data': conn.execute("SELECT id,name,prefix,scopes,admin,expires_at,revoked_at FROM setapi.tokens WHERE kind='api' ORDER BY created_at DESC LIMIT 200").fetchall()}


@router.post('/tokens', status_code=201)
def create_token(body: TokenCreate, response: Response, user=Depends(admin)):
    validate_scopes(body.scopes)
    with db.connection() as conn:
        result = issue(conn, user['id'], body.name, scopes=body.scopes, admin=body.admin, hours=body.hours)
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
