import json
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg import sql
from . import db, tables
from .security import principal, authorize, audit

router = APIRouter(prefix='/api/data', tags=['Data'])


@router.get('/{table}')
def list_records(table: str, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                 sort: str = '-created_at', filter: str = '{}', user=Depends(principal)):
    authorize(user, table, 'read')
    with db.connection() as conn:
        cols = {c['name']: c['type'] for c in tables.columns(conn, table)}
        field = sort.removeprefix('-')
        if field not in cols:
            raise HTTPException(422, 'Unknown sort field')
        try:
            filters = json.loads(filter)
        except ValueError:
            raise HTTPException(422, 'Filter must be a JSON object')
        if not isinstance(filters, dict) or len(filters) > 20:
            raise HTTPException(422, 'Filter must have at most 20 equality conditions')
        clauses, params = [], []
        for key, value in filters.items():
            if key not in cols:
                raise HTTPException(422, 'Unknown filter field')
            clauses.append(sql.SQL('{} IS NOT DISTINCT FROM %s').format(sql.Identifier(key)))
            from psycopg.types.json import Jsonb
            params.append(Jsonb(value) if cols[key] == 'jsonb' else value)
        where = sql.SQL(' AND ').join(clauses) if clauses else sql.SQL('TRUE')
        count = conn.execute(sql.SQL('SELECT count(*) AS total FROM data.{} WHERE {}').format(sql.Identifier(table), where), params).fetchone()['total']
        rows = conn.execute(sql.SQL('SELECT * FROM data.{} WHERE {} ORDER BY {} {} LIMIT %s OFFSET %s').format(
            sql.Identifier(table), where, sql.Identifier(field), sql.SQL('DESC' if sort.startswith('-') else 'ASC')),
            params + [limit, offset]).fetchall()
    return {'data': rows, 'total': count, 'limit': limit, 'offset': offset}


@router.get('/{table}/{record_id}')
def get_record(table: str, record_id: UUID, user=Depends(principal)):
    authorize(user, table, 'read')
    with db.connection() as conn:
        tables.exists(conn, table)
        row = conn.execute(sql.SQL('SELECT * FROM data.{} WHERE id=%s').format(sql.Identifier(table)), (record_id,)).fetchone()
    if not row:
        raise HTTPException(404, 'Record not found')
    return {'data': row}


@router.post('/{table}', status_code=201)
def create_record(table: str, body: dict, user=Depends(principal)):
    authorize(user, table, 'create')
    with db.connection() as conn:
        values = tables.values(conn, table, body)
        row = conn.execute(sql.SQL('INSERT INTO data.{} ({}) VALUES ({}) RETURNING *').format(
            sql.Identifier(table), sql.SQL(',').join(map(sql.Identifier, values)),
            sql.SQL(',').join(sql.Placeholder() for _ in values)), list(values.values())).fetchone()
        tables.event(conn, table, 'created', row['id'])
        audit(conn, user, 'record.create', table, {'id': str(row['id'])})
    return {'data': row}


@router.patch('/{table}/{record_id}')
def update_record(table: str, record_id: UUID, body: dict, user=Depends(principal)):
    authorize(user, table, 'update')
    with db.connection() as conn:
        values = tables.values(conn, table, body)
        setters = sql.SQL(',').join(sql.SQL('{}=%s').format(sql.Identifier(k)) for k in values)
        row = conn.execute(sql.SQL('UPDATE data.{} SET {},updated_at=now() WHERE id=%s RETURNING *').format(
            sql.Identifier(table), setters), list(values.values()) + [record_id]).fetchone()
        if not row:
            raise HTTPException(404, 'Record not found')
        tables.event(conn, table, 'updated', record_id)
        audit(conn, user, 'record.update', table, {'id': str(record_id)})
    return {'data': row}


@router.delete('/{table}/{record_id}', status_code=204)
def delete_record(table: str, record_id: UUID, user=Depends(principal)):
    authorize(user, table, 'delete')
    with db.connection() as conn:
        tables.exists(conn, table)
        row = conn.execute(sql.SQL('DELETE FROM data.{} WHERE id=%s RETURNING id').format(sql.Identifier(table)), (record_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'Record not found')
        tables.event(conn, table, 'deleted', record_id)
        audit(conn, user, 'record.delete', table, {'id': str(record_id)})
