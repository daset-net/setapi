import hashlib
import secrets
from datetime import datetime, timezone, timedelta
from fastapi import HTTPException, Request, Depends
from argon2 import PasswordHasher
from psycopg.types.json import Jsonb
from . import db
from .config import settings

passwords = PasswordHasher()
DUMMY_HASH = passwords.hash(secrets.token_urlsafe(24))
ACTIONS = {'read', 'create', 'update', 'delete'}


def validate_scopes(scopes):
    from .tables import identifier
    for table, actions in scopes.items():
        identifier(table)
        if not set(actions) <= ACTIONS:
            raise HTTPException(422, 'Invalid scope action')
    return scopes


def issue(conn, user_id, name, kind='api', scopes=None, admin=False, hours=720):
    token = 'set_' + secrets.token_urlsafe(40)
    row = conn.execute('''INSERT INTO setapi.tokens(user_id,name,digest,prefix,kind,scopes,admin,expires_at)
      VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id,name,prefix,expires_at''',
      (user_id, name, hashlib.sha256(token.encode()).hexdigest(), token[:12], kind,
       Jsonb(scopes or {}), admin, datetime.now(timezone.utc) + timedelta(hours=hours))).fetchone()
    return dict(row, token=token)


def authenticate(raw):
    if not raw or len(raw) > 256:
        raise HTTPException(401, 'Authentication required')
    with db.connection() as conn:
        row = conn.execute('''SELECT t.id AS token_id,t.scopes,t.admin,t.kind,u.id,u.email,u.role,
          u.scopes AS user_scopes FROM setapi.tokens t JOIN setapi.users u ON u.id=t.user_id
          WHERE t.digest=%s AND t.revoked_at IS NULL AND t.expires_at>now() AND u.active''',
          (hashlib.sha256(raw.encode()).hexdigest(),)).fetchone()
    if not row:
        raise HTTPException(401, 'Invalid or expired token')
    return row


def principal(request: Request):
    header = request.headers.get('authorization', '')
    if header.startswith('Bearer '):
        return authenticate(header[7:])
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        if request.headers.get('origin') != settings().public_url or request.headers.get('x-setapi-csrf') != '1':
            raise HTTPException(403, 'Origin / CSRF validation failed')
    return authenticate(request.cookies.get('setapi_session'))


def is_admin(user):
    return user['role'] == 'admin' and user['admin']


def admin(user=Depends(principal)):
    if not is_admin(user):
        raise HTTPException(403, 'Administrator token required')
    return user


def allowed(user, table, action):
    if is_admin(user):
        return True
    token_allowed = action in user['scopes'].get(table, [])
    return token_allowed and (user['role'] == 'admin' or action in user['user_scopes'].get(table, []))


def authorize(user, table, action):
    if not allowed(user, table, action):
        raise HTTPException(403, 'This token does not allow that operation')


def audit(conn, user, action, resource, details=None):
    conn.execute('INSERT INTO setapi.audit(user_id,action,resource,details) VALUES(%s,%s,%s,%s)',
                 (user['id'], action, resource, Jsonb(details or {})))
