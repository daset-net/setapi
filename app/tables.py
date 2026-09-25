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
    exists(conn, table)
    return conn.execute('''SELECT column_name AS name,data_type AS type,is_nullable='YES' AS nullable,
      column_default AS default_value FROM information_schema.columns
      WHERE table_schema='data' AND table_name=%s ORDER BY ordinal_position''', (table,)).fetchall()


def values(conn, table, body):
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
