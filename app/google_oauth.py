"""Admin-only, session-bound Google Drive authorization."""
import base64
import hashlib
import re
import secrets
from urllib.parse import urlencode
from uuid import UUID
import httpx
from psycopg import errors
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from . import db, storage
from .config import settings
from .security import admin, audit

router = APIRouter(prefix='/api/integrations/google', tags=['Google Drive OAuth'])
SCOPE = 'https://www.googleapis.com/auth/drive.file'
CALLBACK = '/api/integrations/google/callback'


def client_config():
    with db.connection() as conn:
        row = conn.execute("SELECT value_encrypted FROM setapi.integrations WHERE name='google'").fetchone()
    return storage.decrypt(row['value_encrypted']) if row else {}


def session_admin(user=Depends(admin)):
    if user['kind'] != 'session':
        raise HTTPException(403, 'Entre no painel para conectar sua conta Google.')
    return user


@router.get('/config')
def get_config(user=Depends(admin)):
    config = client_config()
    return {'configured': bool(config), 'client_id': config.get('client_id', ''),
            'redirect_uri': settings().public_url + CALLBACK}


class ClientConfig(BaseModel):
    client_id: str = Field(min_length=10, max_length=250)
    client_secret: str = Field(default='', max_length=500)


@router.put('/config')
def put_config(body: ClientConfig, user=Depends(admin)):
    if not re.fullmatch(r'[A-Za-z0-9_-]+\.apps\.googleusercontent\.com', body.client_id):
        raise HTTPException(422, 'Client ID do Google inválido.')
    old = client_config()
    secret = body.client_secret or (old.get('client_secret') if old.get('client_id') == body.client_id else None)
    if not secret or any(c.isspace() for c in secret):
        raise HTTPException(422, 'Informe o Client Secret do aplicativo Google.')
    with db.connection() as conn:
        conn.execute("INSERT INTO setapi.integrations(name,value_encrypted) VALUES('google',%s) ON CONFLICT(name) DO UPDATE SET value_encrypted=EXCLUDED.value_encrypted",
                     (storage.encrypt({'client_id': body.client_id, 'client_secret': secret}),))
        audit(conn, user, 'google.configure', 'google')
    return {'ok': True}


class Connect(BaseModel):
    name: str = Field(default='Google Drive', min_length=1, max_length=100)
    storage_id: UUID | None = None


@router.post('/connect')
def connect(body: Connect, user=Depends(session_admin)):
    config = client_config()
    if not config:
        raise HTTPException(409, 'Configure o aplicativo Google uma vez em Configurar Google.')
    folder = None
    if not body.storage_id:
        with db.connection() as conn:
            if conn.execute('SELECT 1 FROM setapi.storages WHERE name=%s', (body.name,)).fetchone():
                raise HTTPException(409, 'Já existe uma conexão com esse nome. Use Reconectar com Google ou escolha outro nome.')
    if body.storage_id:
        with db.connection() as conn:
            adapter = storage.get(conn, body.storage_id)
        if adapter.provider != 'drive':
            raise HTTPException(422, 'A conexão não é Google Drive.')
        folder = adapter.config['folder_id']
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    callback = settings().public_url + CALLBACK
    payload = dict(config, name=body.name, storage_id=str(body.storage_id) if body.storage_id else None,
                   folder_id=folder, token_id=str(user['token_id']), verifier=verifier, redirect_uri=callback)
    db.cache.setex('setapi:google:state:' + state, 600, storage.encrypt(payload))
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    return {'url': 'https://accounts.google.com/o/oauth2/v2/auth?' + urlencode({
        'client_id': config['client_id'], 'redirect_uri': callback, 'response_type': 'code',
        'scope': SCOPE, 'access_type': 'offline', 'prompt': 'consent select_account',
        'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256'})}


def result(status):
    return RedirectResponse('/?google=' + status + '#storages', status_code=303,
                            headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})


@router.get('/callback', include_in_schema=False)
def callback(request: Request, user=Depends(session_admin)):
    state = request.query_params.get('state', '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{40,100}', state):
        return result('expired')
    key = 'setapi:google:state:' + state
    value = db.cache.get(key)
    if not value:
        return result('expired')
    config = storage.decrypt(value)
    if config['token_id'] != str(user['token_id']):
        return result('expired')
    if db.cache.getdel(key) != value:
        return result('expired')
    if request.query_params.get('error'):
        return result('cancelled')
    code = request.query_params.get('code', '')
    if not code or len(code) > 4096:
        return result('failed')
    try:
        response = httpx.post('https://oauth2.googleapis.com/token', data={
            'grant_type': 'authorization_code', 'code': code, 'client_id': config['client_id'],
            'client_secret': config['client_secret'], 'redirect_uri': config['redirect_uri'],
            'code_verifier': config['verifier']}, timeout=30)
        response.raise_for_status()
        tokens = response.json()
        if not tokens.get('refresh_token') or SCOPE not in tokens.get('scope', '').split():
            return result('permission')
        headers = {'Authorization': 'Bearer ' + tokens['access_token']}
        if config['folder_id']:
            response = httpx.get('https://www.googleapis.com/drive/v3/files/' + config['folder_id'],
                headers=headers, params={'fields': 'id,mimeType,capabilities(canAddChildren)', 'supportsAllDrives': 'true'}, timeout=30)
            response.raise_for_status()
            folder = response.json()
            if folder.get('mimeType') != 'application/vnd.google-apps.folder' or not folder.get('capabilities', {}).get('canAddChildren'):
                return result('folder')
        else:
            response = httpx.post('https://www.googleapis.com/drive/v3/files', headers=headers,
                params={'fields': 'id'}, json={'name': 'SETAPI — ' + config['name'],
                'mimeType': 'application/vnd.google-apps.folder'}, timeout=30)
            response.raise_for_status()
            folder = response.json()
        saved = {k: config[k] for k in ('client_id', 'client_secret')}
        saved.update(refresh_token=tokens['refresh_token'], folder_id=folder['id'])
        storage.validate('drive', saved)
        with db.connection() as conn:
            if config['storage_id']:
                row = conn.execute("UPDATE setapi.storages SET config_encrypted=%s WHERE id=%s AND provider='drive' RETURNING id",
                    (storage.encrypt(saved), config['storage_id'])).fetchone()
                if not row:
                    return result('failed')
            else:
                row = conn.execute("INSERT INTO setapi.storages(name,provider,config_encrypted) VALUES(%s,'drive',%s) RETURNING id",
                    (config['name'], storage.encrypt(saved))).fetchone()
            audit(conn, user, 'google.connect', str(row['id']))
        return result('connected')
    except (httpx.HTTPError, KeyError, ValueError, errors.UniqueViolation):
        # Never expose authorization codes, tokens or provider response bodies.
        return result('failed')
