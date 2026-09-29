"""Administrator notifications through an agent inbox provider, configured by the global administrator."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from . import db, storage, mail, mail_providers as providers
from .security import admin, audit, rate_limit

router = APIRouter(prefix='/api/mail', tags=['E-mail'])


def discover(provider, key):
    if provider not in providers.PROVIDERS:
        raise HTTPException(422, 'Provedor de e-mail desconhecido.')
    try:
        return providers.inboxes(provider, key)
    except providers.ProviderError as exc:
        raise HTTPException(422, str(exc))
    except (KeyError, TypeError, ValueError, AttributeError):
        raise HTTPException(502, 'O provedor respondeu num formato inesperado.')


def key_for(provider, api_key):
    key = api_key.strip()
    if key:
        return key
    current = mail.provider_config()
    # Reuse the saved key so changing the inbox never requires pasting it again.
    if not current or current['provider'] != provider:
        raise HTTPException(422, 'Informe a API key.')
    return current['api_key']


@router.get('/providers')
def list_providers(user=Depends(admin)):
    return {'data': [{'id': k, 'label': v['label']} for k, v in providers.PROVIDERS.items()]}


@router.get('/config')
def get_config(user=Depends(admin)):
    with db.connection() as conn:
        recipients = [r['email'] for r in conn.execute(mail.ADMINS + ' ORDER BY email').fetchall()]
    config = mail.provider_config()
    if not config:
        return {'configured': False, 'recipients': recipients}
    return {'configured': True, 'provider': config['provider'], 'sender': config['sender'], 'inbox_id': config['inbox_id'],
            'key_hint': '…' + config['api_key'][-4:], 'recipients': recipients}


class Discover(BaseModel):
    model_config = ConfigDict(extra='forbid')
    provider: str
    api_key: str = Field(default='', max_length=500)


@router.post('/discover')
def discover_inboxes(body: Discover, user=Depends(admin)):
    rate_limit('setapi:mail:discover:' + str(user['id']), 30)
    return {'data': discover(body.provider, key_for(body.provider, body.api_key))}


class MailConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    provider: str
    api_key: str = Field(default='', max_length=500)
    inbox_id: str = Field(min_length=1, max_length=254)


@router.put('/config')
def put_config(body: MailConfig, user=Depends(admin)):
    key = key_for(body.provider, body.api_key)
    if any(c.isspace() for c in key):
        raise HTTPException(422, 'A API key não pode ter espaços.')
    # The inbox must be one the provider confirms for this key, never free text.
    chosen = next((i for i in discover(body.provider, key) if i['id'] == body.inbox_id), None)
    if not chosen:
        raise HTTPException(422, 'Escolha uma inbox que pertença a essa API key.')
    config = {'provider': body.provider, 'api_key': key, 'inbox_id': chosen['id'], 'sender': chosen['email']}
    with db.connection() as conn:
        conn.execute("INSERT INTO setapi.integrations(name,value_encrypted) VALUES('mail',%s) ON CONFLICT(name) DO UPDATE SET value_encrypted=EXCLUDED.value_encrypted",
                     (storage.encrypt(config),))
        audit(conn, user, 'mail.configure', body.provider, {'sender': config['sender']})
    return {'ok': True, 'sender': config['sender']}


@router.delete('/config', status_code=204)
def delete_config(user=Depends(admin)):
    with db.connection() as conn:
        conn.execute("DELETE FROM setapi.integrations WHERE name='mail'")
        audit(conn, user, 'mail.remove', 'mail')


@router.post('/test')
def send_test(user=Depends(admin)):
    rate_limit('setapi:mail:test:' + str(user['id']), 5)
    config = mail.provider_config()
    if not config:
        raise HTTPException(409, 'Configure um provedor de e-mail primeiro.')
    try:
        providers.send(config, user['email'], 'SETAPI — e-mail de teste',
                       'Este é um teste do SETAPI. Se você recebeu, as notificações e a recuperação de senha vão chegar neste e-mail.')
    except providers.ProviderError as exc:
        raise HTTPException(502, str(exc))
    return {'ok': True, 'to': user['email']}
