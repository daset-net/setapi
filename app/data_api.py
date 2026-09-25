import json
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg import sql
from . import db, tables, policies
from .security import principal, authorize, audit

router = APIRouter(prefix='/api/data', tags=['Data'])


@router.get('/{table}')
def list_records(table: str, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0, le=10000),
                 sort: str = '-created_at', filter: str = '{}', include_total: bool = True, user=Depends(principal)):
    authorize(user, table, 'read')
    with db.connection() as conn:
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
        where = sql.SQL(' AND ').join(clauses) if clauses else sql.SQL('TRUE')
        if field not in readable and field not in ('created_at','id'):
            raise HTTPException(403,'Sorting by this field is not permitted')
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
def get_record(table: str, record_id: UUID, user=Depends(principal)):
    authorize(user, table, 'read')
    with db.connection() as conn:
        tables.require_managed(conn,table)
        readable=policies.fields(conn,table,user,'read')
        scope,params=policies.constraint(conn,table,user)
        row = conn.execute(sql.SQL('SELECT {} FROM data.{} WHERE id=%s AND {}').format(sql.SQL(',').join(map(sql.Identifier,readable)),sql.Identifier(table),scope), [record_id]+params).fetchone()
    if not row:
        raise HTTPException(404, 'Record not found')
    return {'data': row}


@router.post('/{table}', status_code=201)
def create_record(table: str, body: dict, user=Depends(principal)):
    authorize(user, table, 'create')
    with db.connection() as conn:
        tables.require_managed(conn,table)
        policies.check_references(conn,table,user,body)
        values = tables.values(conn, table, policies.writing(conn,table,user,body,create=True))
        row = conn.execute(sql.SQL('INSERT INTO data.{} ({}) VALUES ({}) RETURNING *').format(
            sql.Identifier(table), sql.SQL(',').join(map(sql.Identifier, values)),
            sql.SQL(',').join(sql.Placeholder() for _ in values)), list(values.values())).fetchone()
        readable=policies.fields(conn,table,user,'read')
        row={k:v for k,v in row.items() if k in readable}
        audit(conn, user, 'record.create', table, {'id': str(row['id'])})
    return {'data': row}


@router.patch('/{table}/{record_id}')
def update_record(table: str, record_id: UUID, body: dict, user=Depends(principal)):
    authorize(user, table, 'update')
    with db.connection() as conn:
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
        audit(conn, user, 'record.update', table, {'id': str(record_id)})
    return {'data': row}


@router.delete('/{table}/{record_id}', status_code=204)
def delete_record(table: str, record_id: UUID, user=Depends(principal)):
    authorize(user, table, 'delete')
    with db.connection() as conn:
        tables.require_managed(conn,table)
        tables.exists(conn, table)
        scope,params=policies.constraint(conn,table,user)
        row = conn.execute(sql.SQL('DELETE FROM data.{} WHERE id=%s AND {} RETURNING id').format(sql.Identifier(table),scope), [record_id]+params).fetchone()
        if not row:
            raise HTTPException(404, 'Record not found')
        audit(conn, user, 'record.delete', table, {'id': str(record_id)})
