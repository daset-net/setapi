"""Restore one organization's tables from one of its own backups, in the live database.

The worker first saves a safety backup of the current state, then replaces every table of the organization
by the tables of the chosen backup in a single transaction: either everything is restored or nothing changes.
"""
import json
import logging
import re
import subprocess
import tarfile
import tempfile
from pathlib import Path
from psycopg import sql
from psycopg.types.json import Jsonb
from . import db, storage, tables
from .backup import create_backup, decrypt_file
from .config import settings

log = logging.getLogger('setapi.restore')
# pg_restore --list lines: "<id>; <oid> <oid> <TYPE> <schema> <table or object> [<name>] <owner>".
KINDS = ('TABLE DATA', 'FK CONSTRAINT', 'ROW SECURITY', 'CONSTRAINT', 'TRIGGER', 'POLICY', 'INDEX', 'TABLE', 'DEFAULT', 'COMMENT')
ENTRY = re.compile(r'^\d+; \d+ \d+ (?P<kind>' + '|'.join(KINDS) + r') (?P<schema>\S+) (?P<name>\S+)(?: (?P<rest>.*))?$')
MANAGED_POLICY = re.compile(r'^setapi_read_(allow|boundary)\b')


class RestoreError(Exception):
    """A message safe to show in the panel."""


def plan(dump, prefix, folder):
    """Entries to restore: only objects of this organization's tables in the data schema."""
    listing = subprocess.run(['pg_restore', '--list', str(dump)], capture_output=True, text=True, timeout=120)
    if listing.returncode:
        raise RestoreError('O arquivo do backup não é um dump válido do PostgreSQL.')
    keep, restored = [], set()
    for line in listing.stdout.splitlines():
        if not line.strip() or line.startswith(';'):
            continue
        entry = ENTRY.match(line)
        if not entry:
            raise RestoreError('O backup contém um objeto que não pode ser restaurado.')
        # Index entries carry the index name; comments read "COLUMN <table>.<field>"; every other kind names its table first.
        target = (entry['rest'] or '').split(' ')[0] if entry['kind'] == 'COMMENT' else entry['name']
        if entry['schema'] != 'data' or (entry['kind'] != 'INDEX' and not target.startswith(prefix)):
            raise RestoreError('O backup contém tabelas que não pertencem a esta organização.')
        # Read policies reference this server's PostgREST roles; they are recreated afterwards.
        if entry['kind'] == 'POLICY' and MANAGED_POLICY.match(entry['rest'] or ''):
            continue
        if entry['kind'] == 'TABLE':
            restored.add(entry['name'])
        keep.append(line)
    (folder / 'restore.list').write_text('\n'.join(keep) + '\n')
    return restored


def restore(job):
    """Run one queued restore. Returns the id of the safety backup taken before it."""
    with db.connection() as conn:
        backup = conn.execute("SELECT * FROM setapi.backups WHERE id=%s AND organization_id=%s AND status='completed'",
                              (job['backup_id'], job['organization_id'])).fetchone()
        if not backup:
            raise RestoreError('Este ponto de restauração não está mais disponível.')
        prefix = conn.execute('SELECT table_prefix FROM setapi.organizations WHERE id=%s', (job['organization_id'],)).fetchone()['table_prefix']
        adapter = storage.get(conn, backup['storage_id'], job['organization_id'])
        safety = conn.execute("INSERT INTO setapi.backups(storage_id,organization_id,status,label) VALUES(%s,%s,'running','Antes da restauração') RETURNING *",
                              (backup['storage_id'], job['organization_id'])).fetchone()
    # 1. Safety backup of the current state: without it, nothing is touched.
    try:
        key, size, checksum = create_backup(safety)
    except Exception:
        with db.connection() as conn:
            conn.execute("UPDATE setapi.backups SET status='failed',error='Safety backup failed',finished_at=now() WHERE id=%s", (safety['id'],))
        raise RestoreError('Não foi possível salvar o estado atual antes de restaurar. Nada foi alterado.')
    with db.connection() as conn:
        conn.execute("UPDATE setapi.backups SET status='completed',object_key=%s,size=%s,checksum=%s,finished_at=now() WHERE id=%s", (key, size, checksum, safety['id']))
    with tempfile.TemporaryDirectory(prefix='setapi-org-restore-') as folder:
        folder = Path(folder)
        # 2. Download, decrypt and check the chosen backup belongs to this organization.
        try:
            adapter.download(backup['object_key'], folder / 'backup.setapi')
            decrypt_file(folder / 'backup.setapi', folder / 'bundle.tar', settings().encryption_key)
            with tarfile.open(folder / 'bundle.tar') as archive:
                archive.extractall(folder, members=[m for m in archive.getmembers() if m.name in ('database.dump', 'config.json') and m.isfile()], filter='data')
            config = json.loads((folder / 'config.json').read_text())
        except Exception:
            raise RestoreError('Não foi possível baixar ou abrir o arquivo do backup no storage.')
        if config.get('scope') != 'organization' or str(config['organization']['id']) != str(job['organization_id']) or config['organization']['table_prefix'] != prefix:
            raise RestoreError('Este backup não é desta organização.')
        restored = plan(folder / 'database.dump', prefix, folder)
        script = subprocess.run(['pg_restore', '--use-list', str(folder / 'restore.list'), '--no-owner', '--no-acl', '--file', str(folder / 'restore.sql'), str(folder / 'database.dump')],
                                capture_output=True, timeout=600)
        if script.returncode:
            raise RestoreError('Não foi possível preparar a restauração.')
        # 3. One transaction: drop every current table of the organization, then recreate the backup's.
        with db.connection() as conn:
            current = [r['name'] for r in conn.execute("SELECT table_name AS name FROM information_schema.tables WHERE table_schema='data' AND table_type='BASE TABLE' AND starts_with(table_name,%s)", (prefix,)).fetchall()]
        drops = ''.join(f'DROP TABLE IF EXISTS data."{name}" CASCADE;\n' for name in current if tables.PREFIXED.match(name))
        (folder / 'full.sql').write_text("SET lock_timeout = '30s';\n" + drops + (folder / 'restore.sql').read_text())
        run = subprocess.run(['psql', '--no-psqlrc', '--quiet', '--single-transaction', '-v', 'ON_ERROR_STOP=1', '-f', str(folder / 'full.sql')],
                             env=db.pg_environment(settings().database_url), capture_output=True, timeout=1800)
        if run.returncode:
            log.error('Organization restore failed in psql (exit %s)', run.returncode)
            raise RestoreError('A restauração falhou e foi desfeita: os dados continuam como estavam.')
    # 4. Field policies from the backup, change capture, indexes and PostgREST access for the restored tables.
    with db.connection() as conn:
        conn.execute('DELETE FROM setapi.policies WHERE starts_with(table_name,%s)', (prefix,))
        for rule in config.get('policies', []):
            if rule['table_name'] in restored:
                conn.execute('INSERT INTO setapi.policies(table_name,owner_column,tenant_column,read_fields,write_fields) VALUES(%s,%s,%s,%s,%s)',
                             (rule['table_name'], rule['owner_column'], rule['tenant_column'], Jsonb(rule['read_fields']), Jsonb(rule['write_fields'])))
        for name in sorted(restored):
            tables.manage(conn, name)
            tables.event(conn, name, 'schema', name)
        conn.execute("NOTIFY pgrst, 'reload schema'")
    return safety['id']
