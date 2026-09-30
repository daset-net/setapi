import json
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg import sql
from . import db, tables, policies, postgrest
from .security import principal, authorize, audit

router = APIRouter(prefix='/api/data', tags=['Data'])


@router.get('/{table}')
def list_records(table: str, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0, le=10000),
                 sort: str = Query('-created_at', description='Field to sort by; prefix with - for descending.'),
                 filter: str = Query('{}', description='JSON object of equality conditions, e.g. {"status":"ativo"}; at most 20.'),
                 search: str = Query('', max_length=200, description='Text contained in any readable field (or in search_field), ignoring case, accents and punctuation.'),
                 search_field: str = Query('', description='Limit the search to this field.'),
                 include_total: bool = True, organization_id: UUID | None = Query(None, description='Organization whose tables to use; omit for the platform. Organization users always work in their own.'), user=Depends(principal)):
    """List records of a table, paginated with limit and offset."""
    with db.connection() as conn:
        name, table = table, tables.target(conn, user, table, organization_id)
        authorize(user, name, 'read', table)
        tables.require_managed(conn,table)
        columns=tables.columns(conn,table)
        cols = {c['name']: c['type'] for c in columns}
        readable = policies.fields(conn,table,user,'read',columns)
        field = sort.removeprefix('-')
        if field not in cols:
            raise HTTPException(422, 'Unknown sort field')
        try:
            filters = json.loads(filter)
        except ValueError:
            raise HTTPException(422, 'Filter must be a JSON object')
        if not isinstance(filters, dict) or len(filters) > 20:
            raise HTTPException(422, 'Filter must have at most 20 equality conditions')
        scope, params = policies.constraint(conn,table,user)
        clauses = [scope]
        for key, value in filters.items():
            if key not in readable:
                raise HTTPException(422, 'Unknown filter field')
            clauses.append(sql.SQL('{} IS NOT DISTINCT FROM %s').format(sql.Identifier(key)))
            from psycopg.types.json import Jsonb
            params.append(Jsonb(value) if cols[key] == 'jsonb' else value)
        if search.strip():
            if search_field and search_field not in readable:
                raise HTTPException(422, 'Unknown search field')
            fields = [search_field] if search_field else readable
            clauses.append(sql.SQL('({})').format(sql.SQL(' OR ').join(
                sql.SQL("setapi.search_text({}::text) LIKE '%%' || setapi.search_text(%s) || '%%'").format(sql.Identifier(f)) for f in fields)))
            params.extend([search] * len(fields))
        where = sql.SQL(' AND ').join(clauses) if clauses else sql.SQL('TRUE')
        if field not in readable and field not in ('created_at','id'):
            raise HTTPException(403,'Sorting by this field is not permitted')
        rule = policies.policy(conn, table, user)
        # PostgREST has no accent-free search; those reads use the native engine with the same policy clauses.
        if not postgrest.enabled() or search.strip():
            return _native_list(conn, table, readable, where, params, field, sort, limit, offset, include_total)
    return postgrest.read(user, table, rule, readable, filters, sort, limit, offset, include_total, scope=name)


def _native_list(conn, table, readable, where, params, field, sort, limit, offset, include_total):
    count = conn.execute(sql.SQL('SELECT count(*) AS total FROM data.{} WHERE {}').format(sql.Identifier(table), where), params).fetchone()['total'] if include_total else None
    # Named cursor bounds libpq buffering; oversized pages fail without disclosing partial data.
    rows=[];page_bytes=0
    with conn.cursor(name='page_'+__import__('secrets').token_hex(8)) as cursor:
        cursor.itersize=8
        cursor.execute(sql.SQL('SELECT {} FROM data.{} WHERE {} ORDER BY {} {},id LIMIT %s OFFSET %s').format(
        sql.SQL(',').join(map(sql.Identifier,readable)), sql.Identifier(table), where, sql.Identifier(field), sql.SQL('DESC' if sort.startswith('-') else 'ASC')),
        params + [limit, offset])
        for row in cursor:
            page_bytes+=len(json.dumps(row,default=str).encode())
            if page_bytes>2*1024*1024:
                raise HTTPException(413,'Result page too large; request a smaller limit')
            rows.append(row)
    return {'data': rows, 'total': count, 'limit': limit, 'offset': offset}


@router.get('/{table}/{record_id}')
def get_record(table: str, record_id: UUID, organization_id: UUID | None = Query(None, description='Organization whose tables to use; omit for the platform. Organization users always work in their own.'), user=Depends(principal)):
    with db.connection() as conn:
        name, table = table, tables.target(conn, user, table, organization_id)
        authorize(user, name, 'read', table)
        tables.require_managed(conn,table)
        readable=policies.fields(conn,table,user,'read')
        scope,params=policies.constraint(conn,table,user)
        rule = policies.policy(conn, table, user)
        if not postgrest.enabled():
            row = conn.execute(sql.SQL('SELECT {} FROM data.{} WHERE id=%s AND {}').format(sql.SQL(',').join(map(sql.Identifier,readable)),sql.Identifier(table),scope), [record_id]+params).fetchone()
    if postgrest.enabled():
        result = postgrest.read(user, table, rule, readable, {'id': str(record_id)}, 'id', 1, 0, False, scope=name)
        row = result['data'][0] if result['data'] else None
    if not row:
        raise HTTPException(404, 'Record not found')
    return {'data': row}


@router.post('/{table}', status_code=201)
def create_record(table: str, body: dict, organization_id: UUID | None = Query(None, description='Organization whose tables to use; omit for the platform. Organization users always work in their own.'), user=Depends(principal)):
    with db.connection() as conn:
        name, table = table, tables.target(conn, user, table, organization_id)
        authorize(user, name, 'create', table)
        tables.require_managed(conn,table)
        policies.check_references(conn,table,user,body)
        values = tables.values(conn, table, policies.writing(conn,table,user,body,create=True))
        row = conn.execute(sql.SQL('INSERT INTO data.{} ({}) VALUES ({}) RETURNING *').format(
            sql.Identifier(table), sql.SQL(',').join(map(sql.Identifier, values)),
            sql.SQL(',').join(sql.Placeholder() for _ in values)), list(values.values())).fetchone()
        readable=policies.fields(conn,table,user,'read')
        row={k:v for k,v in row.items() if k in readable}
        audit(conn, user, 'record.create', name, {'id': str(row['id'])})
    return {'data': row}


@router.patch('/{table}/{record_id}')
def update_record(table: str, record_id: UUID, body: dict, organization_id: UUID | None = Query(None, description='Organization whose tables to use; omit for the platform. Organization users always work in their own.'), user=Depends(principal)):
    with db.connection() as conn:
        name, table = table, tables.target(conn, user, table, organization_id)
        authorize(user, name, 'update', table)
        tables.require_managed(conn,table)
        policies.check_references(conn,table,user,body)
        values = tables.values(conn, table, policies.writing(conn,table,user,body))
        scope,params=policies.constraint(conn,table,user)
        setters = sql.SQL(',').join(sql.SQL('{}=%s').format(sql.Identifier(k)) for k in values)
        row = conn.execute(sql.SQL('UPDATE data.{} SET {},updated_at=now() WHERE id=%s AND {} RETURNING *').format(
            sql.Identifier(table), setters,scope), list(values.values()) + [record_id]+params).fetchone()
        if not row:
            raise HTTPException(404, 'Record not found')
        readable=policies.fields(conn,table,user,'read')
        row={k:v for k,v in row.items() if k in readable}
        audit(conn, user, 'record.update', name, {'id': str(record_id)})
    return {'data': row}


@router.delete('/{table}/{record_id}', status_code=204)
def delete_record(table: str, record_id: UUID, organization_id: UUID | None = Query(None, description='Organization whose tables to use; omit for the platform. Organization users always work in their own.'), user=Depends(principal)):
    with db.connection() as conn:
        name, table = table, tables.target(conn, user, table, organization_id)
        authorize(user, name, 'delete', table)
        tables.require_managed(conn,table)
        tables.exists(conn, table)
        scope,params=policies.constraint(conn,table,user)
        row = conn.execute(sql.SQL('DELETE FROM data.{} WHERE id=%s AND {} RETURNING id').format(sql.Identifier(table),scope), [record_id]+params).fetchone()
        if not row:
            raise HTTPException(404, 'Record not found')
        audit(conn, user, 'record.delete', name, {'id': str(record_id)})
