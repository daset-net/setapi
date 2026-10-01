"""Each organization's own inbox: its administrator connects it, and its apps send to anyone through it."""
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field
from . import db, storage, mail, mail_providers as providers
from .mail_api import discover
from .security import principal, is_admin, is_org_admin, audit, rate_limit

router = APIRouter(prefix='/api/organization-mail', tags=['E-mail da organização'])
SEND_PER_HOUR = 300
Address = Annotated[str, Field(max_length=254, pattern=r'^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$')]


def organization(user=Depends(principal), organization_id: UUID | None = Query(None, description='Administrators only: which organization.')):
    """The organization the call acts on: the administrator picks one, an organization administrator gets their own."""
    if is_admin(user):
        if organization_id is None:
            raise HTTPException(422, 'Informe organization_id.')
        with db.connection() as conn:
            if not conn.execute('SELECT 1 FROM setapi.organizations WHERE id=%s AND active', (organization_id,)).fetchone():
                raise HTTPException(404, 'Organization not found')
        return user, str(organization_id)
    if not is_org_admin(user):
        raise HTTPException(403, 'Administrator token required')
    if organization_id is not None and str(organization_id) != str(user['tenant_id']):
        raise HTTPException(404, 'Organization not found')
    return user, str(user['tenant_id'])


def key_for(org, provider, api_key):
    key = api_key.strip()
    if key:
        return key
    current = mail.org_config(org)
    if not current or current['provider'] != provider:
        raise HTTPException(422, 'Informe a API key.')
    return current['api_key']


@router.get('/providers')
def list_providers(ctx=Depends(organization)):
    return {'data': [{'id': k, 'label': v['label']} for k, v in providers.PROVIDERS.items()]}


@router.get('/config')
def get_config(ctx=Depends(organization)):
    config = mail.org_config(ctx[1])
    if not config:
        return {'configured': False}
    return {'configured': True, 'provider': config['provider'], 'sender': config['sender'], 'inbox_id': config['inbox_id'], 'key_hint': '…' + config['api_key'][-4:]}


class Discover(BaseModel):
    model_config = ConfigDict(extra='forbid')
    provider: str
    api_key: str = Field(default='', max_length=500)


@router.post('/discover')
def discover_inboxes(body: Discover, ctx=Depends(organization)):
    rate_limit('setapi:mail:discover:' + str(ctx[0]['id']), 30)
    return {'data': discover(body.provider, key_for(ctx[1], body.provider, body.api_key))}


class MailConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    provider: str
    api_key: str = Field(default='', max_length=500)
    inbox_id: str = Field(min_length=1, max_length=254)


@router.put('/config')
def put_config(body: MailConfig, ctx=Depends(organization)):
    user, org = ctx
    key = key_for(org, body.provider, body.api_key)
    if any(c.isspace() for c in key):
        raise HTTPException(422, 'A API key não pode ter espaços.')
    chosen = next((i for i in discover(body.provider, key) if i['id'] == body.inbox_id), None)
    if not chosen:
        raise HTTPException(422, 'Escolha uma inbox que pertença a essa API key.')
    config = {'provider': body.provider, 'api_key': key, 'inbox_id': chosen['id'], 'sender': chosen['email']}
    with db.connection() as conn:
        conn.execute('INSERT INTO setapi.integrations(name,value_encrypted) VALUES(%s,%s) ON CONFLICT(name) DO UPDATE SET value_encrypted=EXCLUDED.value_encrypted',
                     (mail.org_key(org), storage.encrypt(config)))
        audit(conn, user, 'organization_mail.configure', org, {'sender': config['sender']})
    return {'ok': True, 'sender': config['sender']}


@router.delete('/config', status_code=204)
def delete_config(ctx=Depends(organization)):
    with db.connection() as conn:
        conn.execute('DELETE FROM setapi.integrations WHERE name=%s', (mail.org_key(ctx[1]),))
        audit(conn, ctx[0], 'organization_mail.remove', ctx[1])


@router.post('/test')
def send_test(ctx=Depends(organization)):
    user, org = ctx
    rate_limit('setapi:mail:test:' + str(user['id']), 5)
    config = mail.org_config(org)
    if not config:
        raise HTTPException(409, 'Configure o e-mail da organização primeiro.')
    try:
        providers.send(config, user['email'], 'E-mail de teste', 'Este é um teste do e-mail da organização no SETAPI. Se você recebeu, os aplicativos dela já conseguem enviar por ' + config['sender'] + '.')
    except providers.ProviderError as exc:
        raise HTTPException(502, str(exc))
    return {'ok': True, 'to': user['email']}


class Message(BaseModel):
    model_config = ConfigDict(extra='forbid')
    to: list[Address] = Field(min_length=1, max_length=20)
    subject: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=50000)
    html: str | None = Field(default=None, max_length=200000)
    reply_to: Address | None = None


@router.post('/send', status_code=202)
def send(body: Message, ctx=Depends(organization)):
    """Queue a message from the organization's inbox; the worker delivers it and retries on failure."""
    user, org = ctx
    if not mail.org_config(org):
        raise HTTPException(409, 'Configure o e-mail da organização primeiro.')
    rate_limit('setapi:mail:org-send:' + org, SEND_PER_HOUR, 3600)
    with db.connection() as conn:
        for to in dict.fromkeys(str(t).lower() for t in body.to):
            mail.queue(conn, to, body.subject, body.text, organization_id=org, html=body.html, reply_to=str(body.reply_to) if body.reply_to else None)
    return {'queued': len(set(str(t).lower() for t in body.to))}
