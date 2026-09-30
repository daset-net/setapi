"""Cloudflare R2 from a single API token: the bucket and the S3 credentials are set up automatically."""
import hashlib
import re
import secrets
import time
from uuid import UUID
import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from . import db, storage
from .security import audit, storage_manager
from .storage_api import name_taken

router = APIRouter(prefix='/api/integrations/cloudflare', tags=['Cloudflare R2'])
API = 'https://api.cloudflare.com/client/v4'
BUCKET = re.compile(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]')


class Connect(BaseModel):
    name: str = Field(default='Cloudflare R2', min_length=1, max_length=100)
    api_token: str = Field(min_length=20, max_length=200, description='Cloudflare API token with Workers R2 Storage Write.')
    account_id: str = Field(default='', max_length=64, description='Only needed when the token reaches more than one account.')
    bucket: str = Field(default='', max_length=63, description='Existing or new bucket; a new one is named automatically when empty.')
    organization_id: UUID | None = Field(None, description='Organization that owns the connection; administrators only.')


def call(method, path, token, **kwargs):
    try:
        response = httpx.request(method, API + path, headers={'Authorization': 'Bearer ' + token}, timeout=30, **kwargs)
        data = response.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(502, 'A Cloudflare não respondeu. Tente novamente em instantes.')
    # Cloudflare's own messages ("Authentication error", "bucket already exists") never echo the token.
    errors = [e.get('code') for e in data.get('errors') or []]
    return data, errors, '; '.join(e.get('message', '') for e in data.get('errors') or []) or 'erro desconhecido'


def account_of(token, wanted):
    data, _, message = call('GET', '/accounts', token, params={'per_page': 50})
    if not data.get('success'):
        raise HTTPException(422, 'Token recusado pela Cloudflare: ' + message)
    accounts = data.get('result') or []
    if wanted:
        if not any(a.get('id') == wanted for a in accounts):
            raise HTTPException(422, 'Este token não acessa a conta informada.')
        return wanted
    if len(accounts) == 1:
        return accounts[0]['id']
    if not accounts:
        raise HTTPException(422, 'O token não dá acesso a nenhuma conta. Crie-o com a permissão Workers R2 Storage Write.')
    raise HTTPException(409, 'Este token acessa mais de uma conta. Informe o Account ID: ' +
                        ', '.join(f"{a.get('name')} ({a.get('id')})" for a in accounts[:10]))


def token_id(token, account):
    # Account-owned tokens verify under the account; user tokens under /user.
    for path in (f'/accounts/{account}/tokens/verify', '/user/tokens/verify'):
        data, _, _ = call('GET', path, token)
        result = data.get('result') or {}
        if data.get('success') and result.get('id'):
            if result.get('status') != 'active':
                raise HTTPException(422, 'Este token não está ativo na Cloudflare.')
            return result['id']
    raise HTTPException(422, 'Não foi possível validar o token na Cloudflare.')


def bucket_name(conn, organization, requested):
    if requested:
        if not BUCKET.fullmatch(requested):
            raise HTTPException(422, 'Nome de bucket inválido: use de 3 a 63 letras minúsculas, números e hífens.')
        return requested
    label = 'plataforma'
    if organization:
        label = conn.execute('SELECT name FROM setapi.organizations WHERE id=%s', (organization,)).fetchone()['name']
    slug = re.sub(r'[^a-z0-9]+', '-', label.lower()).strip('-')[:40] or 'org'
    return f'setapi-{slug}-{secrets.token_hex(3)}'


@router.post('/connect', status_code=201)
def connect(body: Connect, user=Depends(storage_manager)):
    token = body.api_token.strip()
    with db.connection() as conn:
        organization = storage.owner(conn, user, body.organization_id)
        if name_taken(conn, organization, body.name):
            raise HTTPException(409, 'Já existe uma conexão com esse nome.')
        bucket = bucket_name(conn, organization, body.bucket.strip())
    account = account_of(token, body.account_id.strip())
    access_key = token_id(token, account)
    data, errors, message = call('POST', f'/accounts/{account}/r2/buckets', token, json={'name': bucket})
    # 10073 (BucketConflict): the bucket already exists in this account, which is fine for a name the user chose.
    if not data.get('success') and not (body.bucket and 10073 in errors):
        raise HTTPException(422, 'A Cloudflare não criou o bucket: ' + message + '. O token precisa da permissão Workers R2 Storage Write.')
    config = {'bucket': bucket, 'region': 'auto', 'endpoint_url': f'https://{account}.r2.cloudflarestorage.com',
              'access_key_id': access_key, 'secret_access_key': hashlib.sha256(token.encode()).hexdigest()}
    storage.validate('r2', config)
    # Credentials derived from a new token can take a few seconds to reach the S3 endpoint.
    for attempt in range(4):
        try:
            storage.Storage('r2', config).test()
            break
        except Exception:
            if attempt == 3:
                raise HTTPException(502, f'O bucket {bucket} foi criado, mas o acesso S3 ainda falha. Confira as permissões do token e tente de novo.')
            time.sleep(2)
    with db.connection() as conn:
        row = conn.execute('INSERT INTO setapi.storages(name,provider,config_encrypted,organization_id) VALUES(%s,%s,%s,%s) RETURNING id,name,provider,organization_id',
                           (body.name, 'r2', storage.encrypt(config), organization)).fetchone()
        audit(conn, user, 'storage.create', str(row['id']), {'provider': 'r2', 'bucket': bucket, 'automatic': True})
    return {**row, 'bucket': bucket}
