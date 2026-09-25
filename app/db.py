from contextlib import contextmanager
import os
from pathlib import Path
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from redis import Redis
from argon2 import PasswordHasher
from .config import settings

pool = None
cache = None


def start():
    global pool, cache
    conf = settings()
    pool = ConnectionPool(conf.database_url, min_size=1, max_size=int(os.getenv("SETAPI_DB_POOL_MAX", "20")), timeout=5, max_waiting=100,
                          kwargs={'row_factory': dict_row, 'options': '-c statement_timeout=15000 -c lock_timeout=5000'}, open=True)
    pool.wait(timeout=30)
    cache = Redis.from_url(conf.redis_url, decode_responses=True, socket_connect_timeout=3, socket_timeout=5)
    cache.ping()
    with pool.connection() as conn:
        conn.execute('SELECT pg_advisory_xact_lock(73288101)')
        conn.execute(Path(__file__).with_name('schema.sql').read_text())
        from . import tables
        rows=conn.execute("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='data' AND c.relkind='r'").fetchall()
        for row in rows:
            cols={c['name']:c['type'] for c in tables.columns(conn,row['relname'])}
            if cols.get('id')=='uuid' and cols.get('created_at')=='timestamp with time zone' and cols.get('updated_at')=='timestamp with time zone':
                from fastapi import HTTPException
                try:tables.manage(conn,row['relname'])
                except HTTPException:pass
        if not conn.execute('SELECT 1 FROM setapi.users LIMIT 1').fetchone():
            if len(conf.bootstrap_password) < 12:
                raise RuntimeError('SETAPI_ADMIN_PASSWORD must contain at least 12 characters for bootstrap.')
            conn.execute('INSERT INTO setapi.users(email,password_hash,role) VALUES (%s,%s,%s)',
                         (conf.bootstrap_email.lower(), PasswordHasher().hash(conf.bootstrap_password), 'admin'))


def stop():
    if pool:
        pool.close()
    if cache:
        cache.close()


@contextmanager
def connection():
    with pool.connection() as conn:
        yield conn


def pg_environment(url):
    import os
    from psycopg.conninfo import conninfo_to_dict
    mapping = {'host':'PGHOST','hostaddr':'PGHOSTADDR','port':'PGPORT','user':'PGUSER','password':'PGPASSWORD',
               'dbname':'PGDATABASE','sslmode':'PGSSLMODE','sslrootcert':'PGSSLROOTCERT','sslcert':'PGSSLCERT','sslkey':'PGSSLKEY',
               'connect_timeout':'PGCONNECT_TIMEOUT','options':'PGOPTIONS','channel_binding':'PGCHANNELBINDING'}
    values = conninfo_to_dict(url)
    unsupported = set(values) - set(mapping)
    if unsupported:
        raise ValueError('Unsupported backup connection parameters: ' + ','.join(sorted(unsupported)))
    env = {k:v for k,v in os.environ.items() if not k.startswith('PG')}
    return {**env, **{mapping[k]:v for k,v in values.items()}, 'PGCONNECT_TIMEOUT':'10'}
