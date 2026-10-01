"""Agent inbox providers reached over HTTPS APIs: discover the inboxes an API key owns and send from one.

Hosts are fixed per provider, so a key can never point SETAPI at an arbitrary address.
"""
from urllib.parse import quote
import httpx

TIMEOUT = 15
REJECTED = 'O provedor recusou a API key. Confira se ela está correta.'


class ProviderError(Exception):
    """The provider refused the key or the request; the message is safe to show."""


def call(method, url, key, **kwargs):
    headers = {'Authorization': 'Bearer ' + key, 'Accept': 'application/json', **kwargs.pop('headers', {})}
    try:
        response = httpx.request(method, url, headers=headers, timeout=TIMEOUT, **kwargs)
    except httpx.HTTPError:
        raise ProviderError('Não foi possível falar com o provedor. Tente novamente.')
    if response.status_code in (401, 403):
        raise ProviderError(REJECTED)
    if response.status_code >= 400:
        # Provider bodies may echo request data; never pass them on.
        raise ProviderError(f'O provedor respondeu com erro {response.status_code}.')
    try:
        return response.json() if response.content else {}
    except ValueError:
        raise ProviderError('O provedor respondeu num formato inesperado.')


def inbox(inbox_id, email, name):
    return {'id': str(inbox_id), 'email': str(email).lower(), 'name': name or ''}


def listed(data, *keys):
    """Inbox lists arrive bare or wrapped, depending on the provider."""
    if isinstance(data, list):
        return data
    for key in keys:
        if isinstance(data.get(key), list):
            return data[key]
    return []


def agentmail_inboxes(key):
    data = call('GET', 'https://api.agentmail.to/v0/inboxes', key, params={'limit': 100})
    return [inbox(i['inbox_id'], i.get('email') or i['inbox_id'], i.get('display_name')) for i in listed(data, 'inboxes')]


def agentmail_send(key, config, to, subject, text, idempotency, html=None, reply_to=None):
    body = {'to': [to], 'subject': subject, 'text': text}
    if html:
        body['html'] = html
    if reply_to:
        body['reply_to'] = reply_to
    call('POST', 'https://api.agentmail.to/v0/inboxes/' + quote(config['inbox_id'], safe='@') + '/messages/send', key, json=body)


def openmail_inboxes(key):
    data = call('GET', 'https://api.openmail.sh/v1/inboxes', key, params={'limit': 100})
    return [inbox(i['id'], i['address'], i.get('displayName')) for i in listed(data, 'data')]


def openmail_send(key, config, to, subject, text, idempotency, html=None, reply_to=None):
    # The key makes a retried queue job deliver once.
    headers = {'Idempotency-Key': idempotency} if idempotency else {}
    call('POST', 'https://api.openmail.sh/v1/inboxes/' + quote(config['inbox_id'], safe='') + '/send', key,
         headers=headers, json={'to': to, 'subject': subject, 'body': text})


def agmail_inboxes(key):
    data = call('GET', 'https://api.agmail.ai/v1/inboxes', key)
    return [inbox(i['id'], i['email'], i.get('display_name')) for i in listed(data, 'data', 'inboxes')]


def agmail_send(key, config, to, subject, text, idempotency, html=None, reply_to=None):
    call('POST', 'https://api.agmail.ai/v1/inboxes/' + quote(config['inbox_id'], safe='') + '/messages', key,
         json={'to': to, 'subject': subject, 'body': text})


PROVIDERS = {
    'agentmail': {'label': 'AgentMail', 'inboxes': agentmail_inboxes, 'send': agentmail_send},
    'openmail': {'label': 'OpenMail', 'inboxes': openmail_inboxes, 'send': openmail_send},
    'agmail': {'label': 'AGMail', 'inboxes': agmail_inboxes, 'send': agmail_send},
}


def inboxes(provider, key):
    return PROVIDERS[provider]['inboxes'](key)


def send(config, to, subject, text, idempotency=None, html=None, reply_to=None):
    PROVIDERS[config['provider']]['send'](config['api_key'], config, to, subject, text, idempotency, html=html, reply_to=reply_to)
