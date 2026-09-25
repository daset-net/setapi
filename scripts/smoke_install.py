"""CI smoke test. Explicitly restricted to a local installation."""
import json
import time
import urllib.request
import http.cookiejar
from pathlib import Path
from env_config import load_env


def main():
    env = load_env(Path(__file__).resolve().parent.parent / '.env')
    base = env['SETAPI_PUBLIC_URL']
    if base != 'http://localhost:8055':
        raise SystemExit('Smoke test is restricted to http://localhost:8055')
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(path, body=None):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
            headers={'Content-Type': 'application/json', 'Origin': base, 'X-SETAPI-CSRF': '1'})
        with opener.open(req, timeout=10) as response:
            return json.load(response)

    call('/api/auth/login', {'email': env['SETAPI_ADMIN_EMAIL'], 'password': env['SETAPI_ADMIN_PASSWORD']})
    status = call('/api/status')
    assert status['worker_alive'] and status['redis']
    call('/api/tables', {'name': 'installation_test', 'columns': [{'name': 'name', 'type': 'text'}]})
    call('/api/data/installation_test', {'name': 'Ready'})
    assert call('/api/data/installation_test')['total'] == 1
    for _ in range(30):
        if call('/api/status')['pending_events'] == 0:
            print('Installation verified: login, PostgreSQL CRUD, Redis and outbox worker.')
            return
        time.sleep(.2)
    raise SystemExit('Outbox worker did not process installation events')


if __name__ == '__main__':
    main()
