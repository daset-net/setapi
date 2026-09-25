import re
from fastapi import HTTPException
from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, ConfigDict

TYPES = {'text': 'text', 'integer': 'bigint', 'decimal': 'numeric', 'boolean': 'boolean',
         'datetime': 'timestamptz', 'date': 'date', 'uuid': 'uuid', 'json': 'jsonb'}
RESERVED = {'id', 'created_at', 'updated_at'}


def identifier(value):
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,47}', value or ''):
        raise HTTPException(422, 'Use lowercase letters, numbers and underscores; maximum 48 characters')
    return value


class Column(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str
    type: str = 'text'
    nullable: bool = True
    unique: bool = False
    references: str | None = None


class Table(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str
    columns: list[Column] = Field(default_factory=list, max_length=100)


def column_sql(column):
    identifier(column.name)
    if column.name in RESERVED or column.type not in TYPES:
        raise HTTPException(422, 'Reserved column name or unsupported type')
    fragment = sql.SQL('{} {}').format(sql.Identifier(column.name), sql.SQL(TYPES[column.type]))
    if not column.nullable:
        fragment += sql.SQL(' NOT NULL')
    if column.unique:
        fragment += sql.SQL(' UNIQUE')
    if column.references:
        identifier(column.references)
        if column.type != 'uuid':
            raise HTTPException(422, 'Relations require a uuid column')
        fragment += sql.SQL(' REFERENCES data.{}(id) ON DELETE RESTRICT').format(sql.Identifier(column.references))
    return fragment


def exists(conn, table):
    identifier(table)
    found = conn.execute("SELECT 1 FROM information_schema.tables WHERE table_schema='data' AND table_name=%s AND table_type='BASE TABLE'", (table,)).fetchone()
    if not found:
        raise HTTPException(404, 'Table not found')


def columns(conn, table):
    identifier(table)
    rows = conn.execute("""SELECT a.attname AS name,
      CASE t.typname WHEN 'bool' THEN 'boolean' WHEN 'int8' THEN 'bigint' WHEN 'timestamptz' THEN 'timestamp with time zone'
      ELSE t.typname END AS type, NOT a.attnotnull AS nullable,
      pg_get_expr(d.adbin,d.adrelid) AS default_value
      FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
      JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_type t ON t.oid=a.atttypid
      LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
      WHERE n.nspname='data' AND c.relname=%s AND c.relkind='r' AND a.attnum>0 AND NOT a.attisdropped
      ORDER BY a.attnum""", (table,)).fetchall()
    if not rows:
        raise HTTPException(404, 'Table not found')
    return rows


def values(conn, table, body):
    import json
    if len(json.dumps(body,default=str).encode())>65536:
        raise HTTPException(413,'Record fields must fit within 64 KiB; use file storage for larger content')
    if not body or len(body) > 100:
        raise HTTPException(422, 'Provide between 1 and 100 fields')
    cols = {c['name']: c['type'] for c in columns(conn, table)}
    result = {}
    for key, value in body.items():
        if key in RESERVED or key not in cols:
            raise HTTPException(422, f'Unknown or read-only field: {key}')
        result[key] = Jsonb(value) if cols[key] == 'jsonb' else value
    return result


def event(conn, table, operation, record_id):
    # IDs only: payloads never disclose record contents over Pub/Sub.
    conn.execute('INSERT INTO setapi.outbox(table_name,event) VALUES(%s,%s)',
                 (table, Jsonb({'table': table, 'operation': operation, 'id': str(record_id)})))


def manage(conn,table):
    cols={c['name']:c['type'] for c in columns(conn,table)}
    if cols.get('id')!='uuid' or cols.get('created_at')!='timestamp with time zone' or cols.get('updated_at')!='timestamp with time zone':
        raise HTTPException(422,'Managed tables require UUID id and timestamp created_at/updated_at fields')
    valid=conn.execute("""SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid=i.indrelid JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_attribute a ON a.attrelid=c.oid AND a.attname='id'
       WHERE n.nspname='data' AND c.relname=%s AND i.indisunique AND i.indisvalid AND i.indnkeyatts=1 AND i.indkey[0]=a.attnum AND i.indpred IS NULL AND a.attnotnull""",(table,)).fetchone()
    if not valid:raise HTTPException(422,'id must be non-null and unique before adoption')
    index='st_'+__import__('hashlib').sha256(table.encode()).hexdigest()[:20]
    conn.execute(sql.SQL('CREATE INDEX IF NOT EXISTS {} ON data.{} (created_at,id)').format(sql.Identifier(index),sql.Identifier(table)))
    conn.execute(sql.SQL('CREATE OR REPLACE TRIGGER setapi_changes AFTER INSERT OR UPDATE OR DELETE ON data.{} FOR EACH ROW EXECUTE FUNCTION setapi.capture_change()').format(sql.Identifier(table)))



def require_managed(conn,table):
    identifier(table)
    if not conn.execute("SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='data' AND c.relname=%s AND t.tgname='setapi_changes' AND t.tgenabled<>'D'",(table,)).fetchone():
        raise HTTPException(409,'Adopt this table before using its data API')
