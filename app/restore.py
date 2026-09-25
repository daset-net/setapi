"""Offline restore into an EMPTY PostgreSQL database; never overwrite a live instance."""
import argparse
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path
import psycopg
from .backup import decrypt_file
from .db import pg_environment


def main():
    parser = argparse.ArgumentParser(description='Restore a SETAPI backup into an empty database.')
    parser.add_argument('backup', type=Path)
    parser.add_argument('--confirm-database', required=True, help='Exact target database name')
    args = parser.parse_args()
    target = os.environ['SETAPI_RESTORE_DATABASE_URL']
    key = os.environ['SETAPI_ENCRYPTION_KEY']
    with psycopg.connect(target) as conn:
        name = conn.execute('SELECT current_database()').fetchone()[0]
        if name != args.confirm_database:
            raise SystemExit('Target database does not match confirmation')
        count = conn.execute("SELECT count(*) FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema')").fetchone()[0]
        if count:
            raise SystemExit('Restore refused: target database must be empty')
    with tempfile.TemporaryDirectory(prefix='setapi-restore-') as folder:
        folder = Path(folder)
        decrypt_file(args.backup, folder / 'bundle.tar', key)
        with tarfile.open(folder / 'bundle.tar') as archive:
            member = archive.getmember('database.dump')
            if not member.isfile():
                raise SystemExit('Invalid backup member')
            with archive.extractfile(member) as src, open(folder / 'database.dump', 'wb') as dst:
                import shutil
                shutil.copyfileobj(src, dst)
        result = subprocess.run(['pg_restore', '--dbname', name, '--no-owner', '--no-acl', '--single-transaction', '--exit-on-error', str(folder / 'database.dump')],
                                env=pg_environment(target), capture_output=True, timeout=1800)
        if result.returncode:
            raise SystemExit('Restore failed; verify client version and database privileges. No partial transaction was committed.')
    # An old backup should not resurrect API/session credentials or pending tasks.
    with psycopg.connect(target) as conn:
        conn.execute('UPDATE setapi.tokens SET revoked_at=now()')
        conn.execute('DELETE FROM setapi.outbox')
        for table in ('action_tokens','mail_queue'):
            if conn.execute("SELECT to_regclass(%s)",('setapi.'+table,)).fetchone()[0]:
                from psycopg import sql
                conn.execute(sql.SQL('DELETE FROM setapi.{}').format(sql.Identifier(table)))
        conn.execute("UPDATE setapi.backups SET status='failed',error='Interrupted by restore' WHERE status IN ('queued','running')")
        conn.execute('UPDATE setapi.schedules SET enabled=false')
    print('Restore completed. Tokens revoked and schedules paused. Use the original encryption key when starting SETAPI.')


if __name__ == '__main__':
    main()
