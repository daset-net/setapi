import os
import urllib.request
from redis import Redis


def main():
    with urllib.request.urlopen('http://127.0.0.1:8055/health/ready', timeout=3) as response:
        if response.status != 200:
            raise SystemExit(1)
    if os.environ.get('SETAPI_RUN_WORKER', 'true').lower() == 'true':
        with Redis.from_url(os.environ['REDIS_URL'], socket_connect_timeout=3, socket_timeout=3) as client:
            if not client.get('setapi:worker:heartbeat'):
                raise SystemExit(1)


if __name__ == '__main__':
    main()
