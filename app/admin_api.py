from datetime import datetime, timezone
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, ConfigDict
from psycopg import errors, sql
from psycopg.types.json import Jsonb
from argon2.exceptions import VerificationError
from . import db, tables, policies, platform_settings
from .organizations import require_active
from .config import settings
from .security import admin, builder, is_org_admin, principal, is_admin, allowed, audit, issue, validate_scopes, passwords, DUMMY_HASH, verify_password, hash_password, rate_limit

router = APIRouter(prefix='/api', tags=['Administration'])


class Login(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=1024)
    otp: str = Field(default='', max_length=100)


@router.post('/auth/login')
def login(body: Login, request: Request, response: Response):
    from .accounts import login_user
    user, token = login_user(body, request, 'panel')
    response.set_cookie('setapi_session', token['token'], httponly=True, secure=settings().cookie_secure, samesite='lax', max_age=platform_settings.current()['session_hours']*3600)
    response.headers['Cache-Control'] = 'no-store'
    return {'user': {'id': user['id'], 'email': user['email'], 'role': user['role']}, 'expires_at': token['expires_at']}


@router.get('/auth/me')
def me(user=Depends(principal)):
    return {'id': user['id'], 'email': user['email'], 'admin': is_admin(user), 'org_admin': is_org_admin(user), 'scopes': user['scopes'], 'mfa_enabled':user['mfa_enabled'], 'audience':user['audience'], 'organization_id':user['tenant_id']}


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
    return {'read_engine': 'postgrest' if postgrest.enabled() else 'native', 'postgres': version, 'redis': db.cache.ping(), 'worker_alive': bool(db.cache.get('setapi:worker:heartbeat')), 'db_pool':db.pool.get_stats(), 'cors_origins': settings().cors_origins, **counts}


ORG = Query(None, description='Organization whose tables to use; omit for the platform. Organization users always work in their own.')


def scope_cleanup(conn, physical, name):
    """A reused table name must not silently inherit an old token's access."""
    if tables.PREFIXED.match(physical):
        owners = "SELECT u.id FROM setapi.users u JOIN setapi.organizations o ON o.id=u.tenant_id WHERE o.table_prefix=%s"
        params = (name, physical[:12])
    else:
        # Platform tables: everyone except organizations that have their own table with this name.
        owners = """SELECT u.id FROM setapi.users u LEFT JOIN setapi.organizations o ON o.id=u.tenant_id
          WHERE o.id IS NULL OR to_regclass('data.'||quote_ident(o.table_prefix||%s)) IS NULL"""
        params = (name, name)
    conn.execute('UPDATE setapi.tokens SET scopes=scopes-%s WHERE user_id IN (' + owners + ')', params)
    conn.execute('UPDATE setapi.users SET scopes=scopes-%s WHERE id IN (' + owners + ')', params)


@router.get('/tables')
def list_tables(organization_id: UUID | None = ORG, user=Depends(principal)):
    with db.connection() as conn:
        organization = tables.context(conn, user, organization_id)
        prefix = tables.prefix_of(conn, organization)
        rows = conn.execute("SELECT table_name AS name FROM information_schema.tables WHERE table_schema='data' AND table_type='BASE TABLE' ORDER BY table_name").fetchall()
        stored = [r['name'] for r in rows]
        chosen = [n for n in stored if n.startswith(prefix)] if prefix else [n for n in stored if not tables.PREFIXED.match(n)]
        if prefix and not is_admin(user):
            # Organization users also see platform tables shared by organization, unless their own has the name.
            own = {tables.logical(n) for n in chosen}
            chosen += [n for n in stored if not tables.PREFIXED.match(n) and n not in own]
        result=[]
        for physical in chosen:
            name = tables.logical(physical)
            if not allowed(user,name,'read',physical):continue
            try:
                readable=policies.fields(conn,physical,user,'read')
            except HTTPException as exc:
                if exc.status_code==403:continue
                raise
            writable=policies.fields(conn,physical,user,'write')
            result.append({'name': name, 'organization_id': organization if physical != name else None,
                           'columns': [dict(c,default_value=None,writable=c['name'] in writable and c['name'] not in tables.RESERVED) for c in tables.columns(conn,physical) if c['name'] in readable]})
        return {'data':sorted(result, key=lambda t: t['name'])}


@router.post('/tables', status_code=201)
def create_table(body: tables.Table, organization_id: UUID | None = ORG, user=Depends(builder)):
    """Create a table with id, created_at, updated_at and the given fields. Its records are served at /api/data/{name}. With organization_id the table belongs to that organization."""
    tables.identifier(body.name)
    if len({c.name for c in body.columns}) != len(body.columns):
        raise HTTPException(422, 'Duplicate columns')
    with db.connection() as conn:
        organization = tables.context(conn, user, organization_id)
        prefix = tables.prefix_of(conn, organization)
        if organization and body.organization_isolated:
            raise HTTPException(422, 'An organization table already belongs only to that organization; leave organization_isolated false')
        physical = tables.target(conn, user, body.name, organization_id, new=True)
        definitions = [sql.SQL('id uuid PRIMARY KEY DEFAULT gen_random_uuid()'),
                       sql.SQL('created_at timestamptz NOT NULL DEFAULT now()'),
                       sql.SQL('updated_at timestamptz NOT NULL DEFAULT now()')]
        if body.organization_isolated:
            if any(c.name=='organization_id' for c in body.columns):raise HTTPException(422,'organization_id is created automatically')
            definitions.append(sql.SQL('organization_id uuid REFERENCES setapi.organizations(id) ON DELETE RESTRICT'))
        definitions.extend(tables.column_sql(c, conn, prefix) for c in body.columns)
        conn.execute(sql.SQL('CREATE TABLE data.{} ({})').format(sql.Identifier(physical), sql.SQL(',').join(definitions)))
        for column in body.columns:
            if column.type == 'file':
                tables.mark_file(conn, physical, column.name, column.storage_id)
        readable=['id','created_at','updated_at']+[c.name for c in body.columns]
        writable=[c.name for c in body.columns]
        if body.organization_isolated:
            conn.execute("INSERT INTO setapi.policies VALUES(%s,NULL,'organization_id',%s,%s)",(physical,Jsonb(readable),Jsonb(writable)))
            index='so_'+__import__('hashlib').sha256(physical.encode()).hexdigest()[:20]
            conn.execute(sql.SQL('CREATE INDEX {} ON data.{} (organization_id,created_at,id)').format(sql.Identifier(index),sql.Identifier(physical)))
        elif organization:
            # The organization's panel users get every field by default; restrict it in the table policy.
            conn.execute('INSERT INTO setapi.policies VALUES(%s,NULL,NULL,%s,%s)',(physical,Jsonb(readable),Jsonb(writable)))
        tables.manage(conn,physical)
        audit(conn, user, 'table.create', body.name, dict(body.model_dump(mode='json'), organization_id=str(organization) if organization else None))
        tables.event(conn, physical, 'schema', physical)
    return {'name': body.name, 'endpoint': '/api/data/' + body.name, 'organization_id': organization}


@router.post('/tables/{table}/columns', status_code=201)
def add_column(table: str, body: tables.Column, organization_id: UUID | None = ORG, user=Depends(builder)):
    """Add a field to an existing table."""
    with db.connection() as conn:
        physical = tables.target(conn, user, table, organization_id, structure=True)
        definition = tables.column_sql(body, conn, physical[:12] if physical != table else '')
        conn.execute(sql.SQL('ALTER TABLE data.{} ADD COLUMN {}').format(sql.Identifier(physical), definition))
        if body.type == 'file':
            tables.mark_file(conn, physical, body.name, body.storage_id)
        policies.follow_column(conn, physical, None, body.name)
        audit(conn, user, 'column.create', table, dict(body.model_dump(mode='json'), organization_id=str(organization_id) if organization_id else None))
        tables.event(conn, physical, 'schema', physical)
    return {'ok': True}


class Rename(BaseModel):
    name: str = Field(description='New field name: ' + tables.NAME_RULE + '.')


@router.patch('/tables/{table}/columns/{column}')
def rename_column(table: str, column: str, body: Rename, organization_id: UUID | None = ORG, user=Depends(builder)):
    """Rename a field. Its data is kept."""
    tables.identifier(column)
    tables.identifier(body.name)
    if column in tables.RESERVED or body.name in tables.RESERVED:
        raise HTTPException(422, 'System columns cannot be renamed')
    with db.connection() as conn:
        physical = tables.target(conn, user, table, organization_id, structure=True)
        policies.guard_column(conn, physical, column, body.name)
        conn.execute(sql.SQL('ALTER TABLE data.{} RENAME COLUMN {} TO {}').format(sql.Identifier(physical), sql.Identifier(column), sql.Identifier(body.name)))
        policies.follow_column(conn, physical, column, body.name)
        audit(conn, user, 'column.rename', table, {'old': column, 'new': body.name, 'organization_id': str(organization_id) if organization_id else None})
        tables.event(conn, physical, 'schema', physical)
    return {'ok': True}


@router.delete('/tables/{table}/columns/{column}', status_code=204)
def drop_column(table: str, column: str, confirm: str = Query(description='Must be exactly "table.column", to confirm.'), organization_id: UUID | None = ORG, user=Depends(builder)):
    """Delete a field and its data. System fields cannot be deleted."""
    tables.identifier(column)
    if column in tables.RESERVED or confirm != f'{table}.{column}':
        raise HTTPException(422, 'Confirm the exact table.column; system columns cannot be deleted')
    with db.connection() as conn:
        physical = tables.target(conn, user, table, organization_id, structure=True)
        policies.guard_column(conn, physical, column)
        conn.execute(sql.SQL('ALTER TABLE data.{} DROP COLUMN {} RESTRICT').format(sql.Identifier(physical), sql.Identifier(column)))
        policies.follow_column(conn, physical, column, None)
        audit(conn, user, 'column.delete', table, {'column': column, 'organization_id': str(organization_id) if organization_id else None})
        tables.event(conn, physical, 'schema', physical)


@router.delete('/tables/{table}', status_code=204)
def drop_table(table: str, confirm: str = Query(description='Must be exactly the table name, to confirm.'), organization_id: UUID | None = ORG, user=Depends(builder)):
    """Delete a table and all its records. Fails while other tables reference it."""
    if confirm != table:
        raise HTTPException(422, 'Confirm the exact table name')
    with db.connection() as conn:
        physical = tables.target(conn, user, table, organization_id, structure=True)
        conn.execute(sql.SQL('DROP TABLE data.{} RESTRICT').format(sql.Identifier(physical)))
        conn.execute('DELETE FROM setapi.policies WHERE table_name=%s',(physical,))
        scope_cleanup(conn, physical, table)
        audit(conn, user, 'table.delete', table, {'organization_id': str(organization_id) if organization_id else None})


class UserCreate(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=12, max_length=1024)
    role: str = 'member'
    scopes: dict[str, list[str]] = Field(default_factory=dict)
    audience: str = 'panel'
    tenant_id: UUID | None = None
    org_admin: bool = Field(False, description='Administrator of its organization: manages the structure of that organization tables. Requires tenant_id and the panel audience.')


@router.get('/users')
def users(user=Depends(admin)):
    with db.connection() as conn:
        return {'data': conn.execute('SELECT id,email,role,active,scopes,audience,tenant_id,org_admin,(mfa_secret IS NOT NULL) AS mfa_enabled,created_at FROM setapi.users ORDER BY created_at').fetchall()}


@router.post('/users', status_code=201)
def create_user(body: UserCreate, user=Depends(admin)):
    import re
    if body.role not in ('admin', 'member') or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',body.email):
        raise HTTPException(422, 'Invalid role or email')
    if body.role=='admin' and body.tenant_id is not None:
        raise HTTPException(422,'Global administrators cannot be organization members')
    if body.audience not in ('panel','app') or (body.audience == 'app' and body.role != 'member'):
        raise HTTPException(422, 'App users must be members')
    if body.org_admin and (body.tenant_id is None or body.audience != 'panel'):
        raise HTTPException(422, 'Organization administrators need an organization and panel access')
    validate_scopes(body.scopes)
    encoded = hash_password(body.password)
    with db.connection() as conn:
        require_active(conn,body.tenant_id)
        row = conn.execute('INSERT INTO setapi.users(email,password_hash,role,scopes,audience,tenant_id,org_admin) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id,email,role,org_admin',
            (body.email.lower(), encoded, body.role, Jsonb(body.scopes), body.audience, body.tenant_id, body.org_admin)).fetchone()
        audit(conn, user, 'user.create', str(row['id']))
    return row


class UserUpdate(BaseModel):
    active: bool | None = None
    email: str | None = Field(None, min_length=3, max_length=254)
    password: str | None = Field(None, min_length=12, max_length=1024, description='New password; ends the user sessions and tokens.')


@router.patch('/users/{user_id}')
def update_user(user_id: UUID, body: UserUpdate, user=Depends(admin)):
    """Activate or deactivate an account, change its e-mail or set a new password."""
    import re
    own = user_id == user['id']
    if own and body.active is False:
        raise HTTPException(409, 'Cannot deactivate your own account')
    if own and body.password:
        raise HTTPException(409, 'Change your own password in Account security')
    if body.email is not None and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', body.email):
        raise HTTPException(422, 'Invalid email')
    with db.connection() as conn:
        row = conn.execute('SELECT id,active FROM setapi.users WHERE id=%s FOR UPDATE', (user_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'User not found')
        changes = {}
        if body.email is not None:
            try:
                with conn.transaction():
                    conn.execute('UPDATE setapi.users SET email=%s WHERE id=%s', (body.email.lower(), user_id))
            except errors.UniqueViolation:
                raise HTTPException(409, 'Já existe um usuário com esse e-mail.')
            changes['email'] = body.email.lower()
        if body.password:
            conn.execute('UPDATE setapi.users SET password_hash=%s WHERE id=%s', (hash_password(body.password), user_id))
            changes['password'] = True
        if body.active is not None:
            conn.execute('UPDATE setapi.users SET active=%s WHERE id=%s', (body.active, user_id))
            changes['active'] = body.active
        # A new password or a deactivation ends every session, token and pending link of the account.
        if body.password or body.active is False:
            conn.execute('DELETE FROM setapi.action_tokens WHERE user_id=%s', (user_id,))
            conn.execute('UPDATE setapi.tokens SET revoked_at=now() WHERE user_id=%s AND revoked_at IS NULL', (user_id,))
        audit(conn, user, 'user.active' if list(changes) == ['active'] else 'user.update', str(user_id), changes)
        return conn.execute('SELECT id,email,active FROM setapi.users WHERE id=%s', (user_id,)).fetchone()


@router.delete('/users/{user_id}', status_code=204)
def delete_user(user_id: UUID, user=Depends(admin)):
    """Delete an account with its tokens. Accounts that own stored files must be deactivated instead."""
    if user_id == user['id']:
        raise HTTPException(409, 'Cannot delete your own account')
    with db.connection() as conn:
        if not conn.execute('SELECT 1 FROM setapi.users WHERE id=%s', (user_id,)).fetchone():
            raise HTTPException(404, 'User not found')
        files = conn.execute('SELECT count(*) AS n FROM setapi.files WHERE owner_id=%s', (user_id,)).fetchone()['n']
        if files:
            raise HTTPException(409, f'Este usuário enviou {files} arquivo(s). Exclua os arquivos ou apenas desative a conta.')
        conn.execute('DELETE FROM setapi.action_tokens WHERE user_id=%s', (user_id,))
        conn.execute('DELETE FROM setapi.tokens WHERE user_id=%s', (user_id,))
        conn.execute('DELETE FROM setapi.users WHERE id=%s', (user_id,))
        audit(conn, user, 'user.delete', str(user_id))


class TokenCreate(BaseModel):
    user_id: UUID | None = None
    name: str = Field(min_length=1, max_length=100)
    hours: int | None = Field(default=720, ge=1, le=8760)
    admin: bool = False
    scopes: dict[str, list[str]] = Field(default_factory=dict)


@router.get('/tokens')
def tokens(user=Depends(admin)):
    with db.connection() as conn:
        return {'data': conn.execute("SELECT t.id,t.name,t.prefix,t.scopes,t.admin,t.expires_at,t.revoked_at,t.user_id,u.tenant_id AS organization_id,u.org_admin,(t.secret_encrypted IS NOT NULL) AS revealable FROM setapi.tokens t JOIN setapi.users u ON u.id=t.user_id WHERE t.kind='api' ORDER BY t.created_at DESC LIMIT 200").fetchall()}


@router.get('/tokens/{token_id}/secret')
def reveal_token(token_id: UUID, response: Response, user=Depends(admin)):
    """Show an API token again. Only the global administrator signed in to the panel; never through an API token."""
    if user['kind'] != 'session':
        raise HTTPException(403, 'Exibir tokens exige o login do administrador no painel')
    with db.connection() as conn:
        row = conn.execute("SELECT name,secret_encrypted FROM setapi.tokens WHERE id=%s AND kind='api' AND revoked_at IS NULL AND expires_at>now()", (token_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'Token not found')
        if not row['secret_encrypted']:
            raise HTTPException(409, 'Este token foi criado antes da opção de exibir. Crie um novo para poder exibi-lo depois.')
        audit(conn, user, 'token.reveal', str(token_id), {'name': row['name']})
    from cryptography.fernet import Fernet
    response.headers['Cache-Control'] = 'no-store'
    return {'id': token_id, 'name': row['name'], 'token': Fernet(settings().encryption_key.encode()).decrypt(row['secret_encrypted'].encode()).decode()}


@router.post('/tokens', status_code=201)
def create_token(body: TokenCreate, response: Response, user=Depends(admin)):
    validate_scopes(body.scopes)
    with db.connection() as conn:
        owner_id=body.user_id or user['id']
        owner=conn.execute('SELECT id,role,active,tenant_id,scopes,org_admin FROM setapi.users WHERE id=%s',(owner_id,)).fetchone()
        if not owner or not owner['active']:raise HTTPException(422,'Choose an active token owner')
        require_active(conn,owner['tenant_id'])
        # An administrative token acts as its owner: globally for the global administrator,
        # inside the organization for an organization administrator.
        if body.admin and not (owner['role']=='admin' and owner['tenant_id'] is None) and not owner['org_admin']:
            raise HTTPException(422,'Only administrators can own administrative tokens')
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


@router.delete('/tokens/{token_id}/permanent', status_code=204)
def delete_token(token_id: UUID, user=Depends(admin)):
    """Remove a revoked or expired API token from the list. An active token must be revoked first."""
    with db.connection() as conn:
        row = conn.execute("SELECT name,(revoked_at IS NOT NULL OR expires_at<=now()) AS ended FROM setapi.tokens WHERE id=%s AND kind='api'", (token_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'Token not found')
        if not row['ended']:
            raise HTTPException(409, 'Revogue o token antes de excluí-lo')
        conn.execute('DELETE FROM setapi.tokens WHERE id=%s', (token_id,))
        audit(conn, user, 'token.delete', str(token_id), {'name': row['name']})


@router.get('/audit')
def audit_log(organization_id: UUID | None = Query(None, description='Only activity of this organization: its users, and changes to its tables and storage.'), user=Depends(admin)):
    with db.connection() as conn:
        if organization_id is None:
            return {'data': conn.execute('SELECT * FROM setapi.audit ORDER BY id DESC LIMIT 100').fetchall()}
        return {'data': conn.execute("""SELECT a.* FROM setapi.audit a WHERE a.user_id IN (SELECT id FROM setapi.users WHERE tenant_id=%s)
          OR a.details->>'organization_id'=%s ORDER BY a.id DESC LIMIT 100""", (organization_id, str(organization_id))).fetchall()}


class IndexCreate(BaseModel):
    columns:list[str]=Field(min_length=1,max_length=4)
    unique:bool=False


@router.get('/tables/{table}/indexes')
def indexes(table:str,organization_id: UUID | None = ORG,user=Depends(builder)):
    with db.connection() as conn:
        physical=tables.target(conn,user,table,organization_id,structure=True)
        return {'data':conn.execute("SELECT indexname,indexdef FROM pg_indexes WHERE schemaname='data' AND tablename=%s",(physical,)).fetchall()}


@router.post('/tables/{table}/indexes',status_code=201)
def add_index(table:str,body:IndexCreate,organization_id: UUID | None = ORG,user=Depends(builder)):
    import hashlib
    with db.connection() as conn:
        physical=tables.target(conn,user,table,organization_id,structure=True)
        cols={c['name'] for c in tables.columns(conn,physical)}
        if not set(body.columns)<=cols or len(set(body.columns))!=len(body.columns):raise HTTPException(422,'Unknown or duplicate field')
        name='si_'+hashlib.sha256((physical+str(body.columns)+str(body.unique)).encode()).hexdigest()[:20]
        conn.execute(sql.SQL('CREATE {} INDEX IF NOT EXISTS {} ON data.{} ({})').format(sql.SQL('UNIQUE' if body.unique else ''),sql.Identifier(name),sql.Identifier(physical),sql.SQL(',').join(map(sql.Identifier,body.columns))))
        audit(conn,user,'index.create',table,{'organization_id':str(organization_id) if organization_id else None})
    return {'name':name}


@router.delete('/tables/{table}/indexes/{index}',status_code=204)
def drop_index(table:str,index:str,organization_id: UUID | None = ORG,user=Depends(builder)):
    with db.connection() as conn:
        physical=tables.target(conn,user,table,organization_id,structure=True)
        if not index.startswith('si_') or not conn.execute("SELECT 1 FROM pg_indexes WHERE schemaname='data' AND tablename=%s AND indexname=%s",(physical,index)).fetchone():
            raise HTTPException(422,'Only user-created SETAPI indexes can be removed')
        conn.execute(sql.SQL('DROP INDEX data.{}').format(sql.Identifier(index)))
        audit(conn,user,'index.delete',table,{'organization_id':str(organization_id) if organization_id else None})


class ColumnEdit(BaseModel):
    type:str=Field(description='New type, one of: '+', '.join(tables.TYPES)+'. Existing values are converted.')
    nullable:bool=Field(True,description='false makes the field required.')
    confirm:str=Field(description='Must be exactly "table.column", to confirm.')
    storage_id:UUID|None=Field(None,description='For type file: the storage its files go to; empty uses the default storage.')


@router.put('/tables/{table}/columns/{column}')
def edit_column(table:str,column:str,body:ColumnEdit,organization_id: UUID | None = ORG,user=Depends(builder)):
    """Change a field's type and whether it is required."""
    if column in tables.RESERVED or body.type not in tables.TYPES or body.confirm!=table+'.'+column:
        raise HTTPException(422,'Confirm the exact table.column and a supported type')
    with db.connection() as conn:
        physical=tables.target(conn,user,table,organization_id,structure=True)
        cols={c['name'] for c in tables.columns(conn,physical)}
        if column not in cols:raise HTTPException(404,'Field not found')
        policies.guard_column(conn,physical,column,column)
        conn.execute(sql.SQL('ALTER TABLE data.{} ALTER COLUMN {} TYPE {} USING {}::{}, ALTER COLUMN {} {} NOT NULL').format(sql.Identifier(physical),sql.Identifier(column),sql.SQL(tables.TYPES[body.type]),sql.Identifier(column),sql.SQL(tables.TYPES[body.type]),sql.Identifier(column),sql.SQL('DROP' if body.nullable else 'SET')))
        tables.mark_file(conn,physical,column,body.storage_id,file=body.type=='file')
        audit(conn,user,'column.type',table,{'column':column,'type':body.type,'organization_id':str(organization_id) if organization_id else None})
    return {'ok':True}


class Adopt(BaseModel):
    confirm:str


@router.post('/tables/{table}/adopt')
def adopt_table(table:str,body:Adopt,organization_id: UUID | None = ORG,user=Depends(builder)):
    if body.confirm!=table:raise HTTPException(422,'Confirm the exact table name')
    with db.connection() as conn:
        physical=tables.target(conn,user,table,organization_id,structure=True)
        cols={c['name']:c['type'] for c in tables.columns(conn,physical)}
        if 'id' in cols and cols['id']!='uuid':raise HTTPException(422,'Existing id must be UUID; migrate its relationships before adoption')
        if 'id' not in cols:
            conn.execute(sql.SQL('ALTER TABLE data.{} ADD COLUMN id uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE').format(sql.Identifier(physical)))
        for col in ('created_at','updated_at'):
            if col not in cols:
                conn.execute(sql.SQL('ALTER TABLE data.{} ADD COLUMN {} timestamptz NOT NULL DEFAULT now()').format(sql.Identifier(physical),sql.Identifier(col)))
        conn.execute(sql.SQL('ALTER TABLE data.{} ALTER COLUMN id SET NOT NULL').format(sql.Identifier(physical)))
        unique_name='su_'+__import__('hashlib').sha256(physical.encode()).hexdigest()[:20]
        conn.execute(sql.SQL('CREATE UNIQUE INDEX IF NOT EXISTS {} ON data.{} (id)').format(sql.Identifier(unique_name),sql.Identifier(physical)))
        tables.manage(conn,physical)
        audit(conn,user,'table.adopt',table,{'organization_id':str(organization_id) if organization_id else None})
    return {'ok':True}
