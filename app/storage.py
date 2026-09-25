import json
from pathlib import Path
from urllib.parse import urlparse
import boto3
import httpx
from botocore.config import Config
from cryptography.fernet import Fernet
from fastapi import HTTPException
from .config import settings


def encrypt(config):
    return Fernet(settings().encryption_key.encode()).encrypt(json.dumps(config).encode()).decode()


def decrypt(value):
    return json.loads(Fernet(settings().encryption_key.encode()).decrypt(value.encode()))


def validate(provider, config):
    required = ['bucket', 'access_key_id', 'secret_access_key'] if provider in ('s3', 'r2') else ['client_id', 'client_secret', 'refresh_token', 'folder_id']
    if provider not in ('s3', 'r2', 'drive') or any(not config.get(k) for k in required):
        raise HTTPException(422, 'Missing provider configuration fields')
    if provider == 'r2' and not config.get('endpoint_url'):
        raise HTTPException(422, 'R2 requires its HTTPS S3 endpoint')
    if config.get('endpoint_url') and urlparse(config['endpoint_url']).scheme != 'https':
        raise HTTPException(422, 'Storage endpoints must use HTTPS')


class Storage:
    def __init__(self, provider, config):
        self.provider, self.config = provider, config
        if provider in ('s3', 'r2'):
            self.s3 = boto3.client('s3', endpoint_url=config.get('endpoint_url') or None,
                region_name=config.get('region', 'auto' if provider == 'r2' else 'us-east-1'),
                aws_access_key_id=config['access_key_id'], aws_secret_access_key=config['secret_access_key'],
                config=Config(connect_timeout=10, read_timeout=120, retries={'max_attempts': 3}))

    def drive_headers(self):
        response = httpx.post('https://oauth2.googleapis.com/token', data={
            'grant_type': 'refresh_token', 'client_id': self.config['client_id'],
            'client_secret': self.config['client_secret'], 'refresh_token': self.config['refresh_token']}, timeout=30)
        response.raise_for_status()
        return {'Authorization': 'Bearer ' + response.json()['access_token']}

    def test(self):
        if self.provider in ('s3', 'r2'):
            self.s3.head_bucket(Bucket=self.config['bucket'])
        else:
            r = httpx.get('https://www.googleapis.com/drive/v3/files/' + self.config['folder_id'],
                          headers=self.drive_headers(), params={'fields': 'id,mimeType', 'supportsAllDrives': 'true'}, timeout=30)
            r.raise_for_status()
            if r.json()['mimeType'] != 'application/vnd.google-apps.folder':
                raise ValueError('Drive destination must be a folder')

    def upload(self, path, key, content_type='application/octet-stream'):
        if self.provider in ('s3', 'r2'):
            self.s3.upload_file(str(path), self.config['bucket'], key, ExtraArgs={'ContentType': content_type})
            return key
        headers = self.drive_headers()
        size = Path(path).stat().st_size
        r = httpx.post('https://www.googleapis.com/upload/drive/v3/files',
            params={'uploadType': 'resumable', 'supportsAllDrives': 'true'},
            headers={**headers, 'X-Upload-Content-Type': content_type, 'X-Upload-Content-Length': str(size)},
            json={'name': key.replace('/', '_'), 'parents': [self.config['folder_id']]}, timeout=30)
        r.raise_for_status()
        location = r.headers['location']
        parsed = urlparse(location)
        if parsed.scheme != 'https' or parsed.hostname != 'www.googleapis.com':
            raise ValueError('Unexpected Drive upload location')
        # 8 MiB chunks satisfy Drive's 256 KiB chunk requirement.
        with open(path, 'rb') as stream:
            offset = 0
            while True:
                chunk = stream.read(8 * 1024 * 1024)
                if not chunk:
                    break
                end = offset + len(chunk) - 1
                r = httpx.put(location, headers={**headers, 'Content-Length': str(len(chunk)),
                    'Content-Range': f'bytes {offset}-{end}/{size}'}, content=chunk, timeout=120)
                offset += len(chunk)
                if r.status_code == 308:
                    continue
                r.raise_for_status()
                return r.json()['id']
        raise ValueError('Empty upload or incomplete Drive upload')

    def download(self, key, path):
        if self.provider in ('s3', 'r2'):
            self.s3.download_file(self.config['bucket'], key, str(path))
        else:
            with httpx.stream('GET', 'https://www.googleapis.com/drive/v3/files/' + key,
                params={'alt': 'media', 'supportsAllDrives': 'true'}, headers=self.drive_headers(), timeout=120) as r:
                r.raise_for_status()
                with open(path, 'wb') as stream:
                    for chunk in r.iter_bytes():
                        stream.write(chunk)

    def delete(self, key):
        if self.provider in ('s3', 'r2'):
            self.s3.delete_object(Bucket=self.config['bucket'], Key=key)
        else:
            r = httpx.delete('https://www.googleapis.com/drive/v3/files/' + key,
                headers=self.drive_headers(), params={'supportsAllDrives': 'true'}, timeout=30)
            r.raise_for_status()


def get(conn, storage_id):
    row = conn.execute('SELECT * FROM setapi.storages WHERE id=%s', (storage_id,)).fetchone()
    if not row:
        raise HTTPException(404, 'Storage not found')
    return Storage(row['provider'], decrypt(row['config_encrypted']))
