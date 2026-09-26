"""Private PostgREST read engine; public authentication stays in SETAPI."""
import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
import httpx
from fastapi import HTTPException
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from .config import settings

HTTP = httpx.Client(base_url='http://127.0.0.1:3000', trust_env=False,
                    timeout=httpx.Timeout(20, connect=2), limits=httpx.Limits(max_connections=100, max_keepalive_connections=20))


def enabled():
    engine = os.getenv('SETAPI_READ_ENGINE', 'native')
    if engine not in ('native', 'postgrest'):
        raise RuntimeError('SETAPI_READ_ENGINE must be native or postgrest')
    return engine == 'postgrest'


def names():
    database = conninfo_to_dict(settings().database_url).get('dbname', '')
    if not database:
        raise RuntimeError('PostgREST requires an explicit database name in DATABASE_URL')
    suffix = hashlib.sha256(database.encode()).hexdigest()[:12]
    return 'sr_' + suffix, 'sa_' + suffix


def secret(purpose):
    return hmac.new(settings().encryption_key.encode(), ('setapi-postgrest-v1:' + purpose + ':' + names()[0]).encode(), hashlib.sha256).hexdigest()


def prepare(conn):
    reader, authenticator = names()
    for name, login in ((reader, False), (authenticator, True)):
        if not conn.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (name,)).fetchone():
            conn.execute(sql.SQL('CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS').format(sql.Identifier(name)))
        row = conn.execute('SELECT rolsuper,rolbypassrls,rolcreaterole,rolcreatedb FROM pg_roles WHERE rolname=%s', (name,)).fetchone()
        if any(row.values()):
            raise RuntimeError('Unsafe PostgREST role privileges')
        access = conn.execute('SELECT rolinherit,rolcanlogin FROM pg_roles WHERE rolname=%s', (name,)).fetchone()
        if access['rolinherit'] or (not login and access['rolcanlogin']):
            raise RuntimeError('Unsafe PostgREST role inheritance/login')
        memberships = conn.execute('SELECT parent.rolname FROM pg_auth_members m JOIN pg_roles parent ON parent.oid=m.roleid JOIN pg_roles child ON child.oid=m.member WHERE child.rolname=%s', (name,)).fetchall()
        if any(item['rolname'] != reader or not login for item in memberships):
            raise RuntimeError('Unexpected PostgREST role membership')
        conn.execute(sql.SQL('REVOKE ALL ON ALL TABLES IN SCHEMA data,setapi FROM {}').format(sql.Identifier(name)))
        if login:
            conn.execute(sql.SQL('ALTER ROLE {} LOGIN PASSWORD {}').format(sql.Identifier(name), sql.Literal(secret('database'))))
    conn.execute(sql.SQL("ALTER ROLE {} SET statement_timeout = '15s'").format(sql.Identifier(reader)))
    conn.execute(sql.SQL('GRANT {} TO {}').format(sql.Identifier(reader), sql.Identifier(authenticator)))
    conn.execute(sql.SQL('GRANT USAGE ON SCHEMA data,setapi TO {}').format(sql.Identifier(reader)))
    conn.execute(Path(__file__).with_name('postgrest.sql').read_text())
    conn.execute(sql.SQL('GRANT EXECUTE ON FUNCTION setapi.postgrest_check() TO {}').format(sql.Identifier(reader)))


def protect(conn, table):
    """Typed RLS predicates retain index use; only managed tables are granted."""
    if not enabled():
        return
    reader, _ = names()
    rule = conn.execute('SELECT * FROM setapi.policies WHERE table_name=%s', (table,)).fetchone()
    def claim(name):
        return sql.SQL("(SELECT nullif(current_setting('request.jwt.claims',true),'')::jsonb->>{})").format(sql.Literal(name))
    terms = []
    if rule:
        for field, key in (('owner_column', 'sub'), ('tenant_column', 'tenant')):
            if rule[field]:
                terms.append(sql.SQL('{} = {}::uuid').format(sql.Identifier(rule[field]), claim(key)))
    scoped = sql.SQL(' AND ').join(terms) if terms else sql.SQL('TRUE')
    condition = sql.SQL("{} = {} AND ({} = 'true' OR ({}))").format(claim('table'), sql.Literal(table), claim('root'), scoped)
    conn.execute(sql.SQL('ALTER TABLE data.{} ENABLE ROW LEVEL SECURITY').format(sql.Identifier(table)))
    for name in ('setapi_read_allow', 'setapi_read_boundary'):
        conn.execute(sql.SQL('DROP POLICY IF EXISTS {} ON data.{}').format(sql.Identifier(name), sql.Identifier(table)))
    conn.execute(sql.SQL('CREATE POLICY setapi_read_allow ON data.{} FOR SELECT TO {} USING (true)').format(sql.Identifier(table), sql.Identifier(reader)))
    conn.execute(sql.SQL('CREATE POLICY setapi_read_boundary ON data.{} AS RESTRICTIVE FOR SELECT TO {} USING ({})').format(sql.Identifier(table), sql.Identifier(reader), condition))
    conn.execute(sql.SQL('GRANT SELECT ON data.{} TO {}').format(sql.Identifier(table), sql.Identifier(reader)))
    conn.execute("NOTIFY pgrst, 'reload schema'")


def jwt(user, table, rule):
    from .security import is_admin
    now = int(time.time())
    claims = {'role': names()[0], 'aud': 'setapi-internal', 'iat': now, 'exp': now + 30,
              'sub': str(user['id']), 'token_id': str(user['token_id']),
              'tenant': str(user['tenant_id']) if user.get('tenant_id') else None,
              'root': is_admin(user), 'table': table, 'policy': rule}
    def encode(value):
        return base64.urlsafe_b64encode(value).rstrip(b'=')
    unsigned = b'.'.join(encode(json.dumps(value, separators=(',', ':'), default=str).encode()) for value in ({'alg': 'HS256', 'typ': 'JWT'}, claims))
    return (unsigned + b'.' + encode(hmac.new(secret('jwt').encode(), unsigned, hashlib.sha256).digest())).decode()


def read(user, table, rule, fields, filters, sort, limit, offset, include_total, _retry=0):
    params = [('select', ','.join(fields)), ('order', sort.removeprefix('-') + ('.desc' if sort.startswith('-') else '.asc') + ',id.asc'),
              ('limit', str(limit)), ('offset', str(offset))]
    conditions = []
    def quoted(value):
        return '"' + str(value).replace(chr(92), chr(92) * 2).replace('"', chr(92) + '"') + '"'
    constraints = list(filters.items())
    if rule:
        for column, value in ((rule['owner_column'], user['id']), (rule['tenant_column'], user.get('tenant_id'))):
            if column:
                if value is None:
                    return {'data': [], 'total': 0 if include_total else None, 'limit': limit, 'offset': offset}
                constraints.append((column, str(value)))
    for key, value in constraints:
        if value is None:
            operand = 'is.null'
        elif isinstance(value, bool):
            operand = 'eq.' + str(value).lower()
        elif isinstance(value, (dict, list)):
            operand = 'eq.' + quoted(json.dumps(value, separators=(',', ':')))
        else:
            operand = 'eq.' + quoted(value)
        conditions.append(quoted(key) + '.' + operand)
    if conditions:
        params.append(('and', '(' + ','.join(conditions) + ')'))
    headers = {'Authorization': 'Bearer ' + jwt(user, table, rule)}
    if include_total:
        headers['Prefer'] = 'count=exact'
    try:
        with HTTP.stream('GET', '/' + table, params=params, headers=headers) as response:
            if response.status_code in (400, 404) and _retry < 5:
                error = response.read()
                if len(error) < 8192 and json.loads(error).get('code') in ('PGRST204', 'PGRST205'):
                    time.sleep(.15)
                    return read(user, table, rule, fields, filters, sort, limit, offset, include_total, _retry + 1)
            if response.status_code == 416:
                total = response.headers.get('content-range', '').split('/')[-1]
                if total.isdigit() and offset >= int(total):
                    return {'data': [], 'total': int(total) if include_total else None, 'limit': limit, 'offset': offset}
            if response.status_code >= 400:
                status = response.status_code
                raise HTTPException(403 if status in (401, 403) else 422 if status == 400 else 503,
                                    'Data query rejected' if status < 500 else 'Data engine unavailable')
            chunks = bytearray()
            for chunk in response.iter_bytes(chunk_size=8192):
                chunks.extend(chunk)
                if len(chunks) > 2 * 1024 * 1024:
                    raise HTTPException(413, 'Result page too large; request a smaller limit')
            data = json.loads(chunks)
            total = response.headers.get('content-range', '*/0').split('/')[-1]
            return {'data': data, 'total': int(total) if include_total and total != '*' else None,
                    'limit': limit, 'offset': offset}
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, 'Data engine unavailable', headers={'Retry-After': '2'}) from None


def environment():
    _, authenticator = names()
    values = conninfo_to_dict(settings().database_url)
    values.update(user=authenticator, password=secret('database'))
    threads = int(os.getenv('SETAPI_POSTGREST_THREADS', '1'))
    if not 1 <= threads <= 32:
        raise RuntimeError('SETAPI_POSTGREST_THREADS must be between 1 and 32')
    return {**{key: value for key, value in os.environ.items() if key in ('PATH', 'LD_LIBRARY_PATH', 'LANG', 'TZ', 'SSL_CERT_FILE', 'SSL_CERT_DIR')},
            'GHCRTS': '-N' + str(threads), 'PGRST_DB_URI': make_conninfo(**values), 'PGRST_DB_SCHEMAS': 'data',
            'PGRST_DB_PRE_REQUEST': 'setapi.postgrest_check', 'PGRST_DB_ANON_ROLE': '',
            'PGRST_DB_CONFIG': 'false', 'PGRST_JWT_SECRET': secret('jwt'),
            'PGRST_JWT_AUD': 'setapi-internal', 'PGRST_SERVER_HOST': '127.0.0.1',
            'PGRST_SERVER_PORT': '3000', 'PGRST_SERVER_PROXY_URI': '',
            'PGRST_ADMIN_SERVER_PORT': '3001', 'PGRST_DB_POOL': os.getenv('SETAPI_POSTGREST_POOL', '10'),
            'PGRST_DB_POOL_ACQUISITION_TIMEOUT': '5', 'PGRST_DB_MAX_ROWS': '200',
            'PGRST_OPENAPI_MODE': 'disabled', 'PGRST_LOG_LEVEL': 'crit'}


def main():
    os.execvpe('postgrest', ['postgrest'], environment())


if __name__ == '__main__':
    main()
