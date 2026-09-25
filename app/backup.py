"""Authenticated streaming encryption for offline, provider-independent backups."""
import base64
import hashlib
import json
import os
import struct
import subprocess
import tarfile
import tempfile
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from .config import settings
from . import db, storage

MAGIC = b'SETAPI01'
CHUNK = 1024 * 1024


def encrypt_file(source, destination, key):
    aes = AESGCM(base64.urlsafe_b64decode(key))
    prefix = os.urandom(8)
    with open(source, 'rb') as src, open(destination, 'wb') as dst:
        dst.write(MAGIC + prefix)
        index = 0
        while True:
            data = src.read(CHUNK)
            counter = struct.pack('>I', index)
            encrypted = aes.encrypt(prefix + counter, data, MAGIC + counter)
            dst.write(struct.pack('>I', len(encrypted)) + encrypted)
            if not data:
                break
            index += 1


def decrypt_file(source, destination, key):
    aes = AESGCM(base64.urlsafe_b64decode(key))
    try:
        with open(source, 'rb') as src, open(destination, 'xb') as dst:
            if src.read(8) != MAGIC:
                raise ValueError('Invalid backup header')
            prefix = src.read(8)
            index = 0
            while True:
                length = src.read(4)
                if len(length) != 4:
                    raise ValueError('Truncated backup')
                size = struct.unpack('>I', length)[0]
                if not 16 <= size <= CHUNK + 16:
                    raise ValueError('Invalid chunk')
                data = src.read(size)
                counter = struct.pack('>I', index)
                plain = aes.decrypt(prefix + counter, data, MAGIC + counter)
                if not plain:
                    if src.read(1):
                        raise ValueError('Unexpected trailing data')
                    break
                dst.write(plain)
                index += 1
    except FileExistsError:
        raise
    except Exception:
        Path(destination).unlink(missing_ok=True)
        raise


def create_backup(job):
    with db.connection() as conn:
        adapter = storage.get(conn, job['storage_id'])
        providers = conn.execute('SELECT id,name,provider,config_encrypted FROM setapi.storages').fetchall()
        schedules = conn.execute('SELECT * FROM setapi.schedules').fetchall()
    with tempfile.TemporaryDirectory(prefix='setapi-backup-') as folder:
        folder = Path(folder)
        env = db.pg_environment(settings().database_url)
        result = subprocess.run(['pg_dump', '--format=custom', '--no-owner', '--no-acl', '--file', str(folder / 'database.dump')],
                                env=env, capture_output=True, timeout=1800)
        if result.returncode:
            # libpq errors can contain connection strings. Never return raw stderr.
            raise RuntimeError('pg_dump failed: verify PostgreSQL client version, connectivity and database privileges')
        config = {'format': 1, 'storages': providers, 'schedules': schedules,
                  'note': 'Credentials remain encrypted with SETAPI_ENCRYPTION_KEY. Storage file contents are not included.'}
        (folder / 'config.json').write_text(json.dumps(config, default=str, indent=2))
        with tarfile.open(folder / 'bundle.tar', 'w') as archive:
            for name in ('database.dump', 'config.json'):
                archive.add(folder / name, arcname=name)
        path = folder / 'backup.setapi'
        encrypt_file(folder / 'bundle.tar', path, settings().encryption_key)
        with open(path, 'rb') as stream:
            checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
        key = adapter.upload(path, f'backups/{job["id"]}.setapi')
        return key, path.stat().st_size, checksum
