from fastapi import APIRouter, Depends, HTTPException
from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field
from . import db, tables
from .security import admin, is_admin, audit

router=APIRouter(prefix='/api/tables',tags=['Data access policies'])


def policy(conn,table,user):
    if is_admin(user):return None
    cache=user.setdefault('_policies',{})
    if table not in cache:
        cache[table]=conn.execute('SELECT * FROM setapi.policies WHERE table_name=%s',(table,)).fetchone()
    row=cache[table]
    if not row and user['role']!='admin':
        raise HTTPException(403,'This table has no access policy for members')
    return row


def fields(conn,table,user,action,columns=None):
    columns=columns or tables.columns(conn,table)
    rule=policy(conn,table,user)
    names=[c['name'] for c in columns]
    if rule:
        names=[n for n in names if n in rule['read_fields' if action=='read' else 'write_fields']]
    return names


def constraint(conn,table,user):
    rule=policy(conn,table,user)
    clauses=[];params=[]
    if rule:
        for key,value in [('owner_column',user['id']),('tenant_column',user.get('tenant_id'))]:
            if rule[key]:
                if value is None:return sql.SQL('FALSE'),[]
                clauses.append(sql.SQL('{}=%s').format(sql.Identifier(rule[key])));params.append(value)
    return (sql.SQL(' AND ').join(clauses) if clauses else sql.SQL('TRUE')),params


def writing(conn,table,user,body,create=False):
    rule=policy(conn,table,user)
    result=dict(body)
    if rule:
        if set(body)-set(rule['write_fields']):
            raise HTTPException(403,'Writing one or more fields is not permitted')
        for key,value in [('owner_column',user['id']),('tenant_column',user.get('tenant_id'))]:
            if rule[key]:
                if value is None:raise HTTPException(403,'Tenant membership is required')
                if rule[key] in body:raise HTTPException(403,'Ownership fields are assigned by the server')
                if create:result[rule[key]]=value
    return result


def visible_event(user,event):
    if is_admin(user):return True
    with db.connection() as conn:
        rule=policy(conn,event['table'],user)
    if not rule:return True
    row=event.get('_row',{})
    for key,value in [('owner_column',user['id']),('tenant_column',user.get('tenant_id'))]:
        if rule[key] and (value is None or str(row.get(rule[key]))!=str(value)):
            return False
    return True


class Policy(BaseModel):
    owner_column:str|None=None
    tenant_column:str|None=None
    read_fields:list[str]=Field(default_factory=list,max_length=100)
    write_fields:list[str]=Field(default_factory=list,max_length=100)


@router.get('/{table}/policy')
def get_policy(table:str,user=Depends(admin)):
    with db.connection() as conn:
        tables.exists(conn,table)
        return conn.execute('SELECT * FROM setapi.policies WHERE table_name=%s',(table,)).fetchone() or {'owner_column':None,'tenant_column':None,'read_fields':[],'write_fields':[]}


@router.put('/{table}/policy')
def put_policy(table:str,body:Policy,user=Depends(admin)):
    with db.connection() as conn:
        cols={c['name']:c for c in tables.columns(conn,table)}
        for column in (body.owner_column,body.tenant_column):
            if column and (column not in cols or cols[column]['type']!='uuid' or column in tables.RESERVED):
                raise HTTPException(422,'Ownership columns must be custom UUID fields')
        if body.owner_column and body.owner_column==body.tenant_column:raise HTTPException(422,'Owner and tenant must be different columns')
        if not set(body.read_fields+body.write_fields)<=cols.keys():raise HTTPException(422,'Unknown field')
        if set(body.write_fields)&(tables.RESERVED|{body.owner_column,body.tenant_column}):raise HTTPException(422,'System and ownership fields are read-only')
        if 'id' not in body.read_fields:raise HTTPException(422,'Include id among readable fields')
        conn.execute('''INSERT INTO setapi.policies VALUES(%s,%s,%s,%s,%s) ON CONFLICT(table_name) DO UPDATE
        SET owner_column=EXCLUDED.owner_column,tenant_column=EXCLUDED.tenant_column,read_fields=EXCLUDED.read_fields,write_fields=EXCLUDED.write_fields''',
        (table,body.owner_column,body.tenant_column,Jsonb(body.read_fields),Jsonb(body.write_fields)))
        for col in (body.owner_column,body.tenant_column):
            if col:
                name='sp_'+__import__('hashlib').sha256((table+col).encode()).hexdigest()[:20]
                conn.execute(sql.SQL('CREATE INDEX IF NOT EXISTS {} ON data.{} ({},created_at,id)').format(sql.Identifier(name),sql.Identifier(table),sql.Identifier(col)))
        audit(conn,user,'policy.update',table)
    return {'ok':True}


def protect_column(conn,table,column):
    row=conn.execute('SELECT * FROM setapi.policies WHERE table_name=%s',(table,)).fetchone()
    if row and column in [row['owner_column'],row['tenant_column'],*row['read_fields'],*row['write_fields']]:
        raise HTTPException(409,'Update the access policy before modifying this column')


def routing_rules(user,requested):
    if is_admin(user):return {}
    with db.connection() as conn:
        return {table:policy(conn,table,user) for table in requested}


def skip_event(user,event,rules):
    """Cheap rejection only. Delivery still authenticates and checks current DB policy."""
    rule=rules.get(event.get('table'))
    if not rule:return False
    route=event.get('_row',{})
    for key,value in [('owner_column',user['id']),('tenant_column',user.get('tenant_id'))]:
        column=rule[key]
        if column and column in route and str(route[column])!=str(value):return True
    return False


def channels(user,requested,rules):
    if is_admin(user):return ['setapi:events']
    result=[]
    for table in requested:
        rule=rules.get(table)
        base='setapi:table:'+table
        if rule and rule['owner_column']:base+=':owner:'+str(user['id'])
        elif rule and rule['tenant_column']:base+=':tenant:'+str(user.get('tenant_id'))
        result.append(base)
    return result or ['setapi:empty:'+str(user['id'])]


def check_references(conn,table,user,body):
    if is_admin(user):return
    from .security import allowed
    refs=conn.execute("""SELECT a.attname AS field,n2.nspname AS schema,c2.relname AS target,a2.attname AS target_field,
       cardinality(k.conkey) AS count FROM pg_constraint k
       JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace
       JOIN pg_class c2 ON c2.oid=k.confrelid JOIN pg_namespace n2 ON n2.oid=c2.relnamespace
       JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=k.conkey[1]
       JOIN pg_attribute a2 ON a2.attrelid=c2.oid AND a2.attnum=k.confkey[1]
       WHERE k.contype='f' AND n.nspname='data' AND c.relname=%s""",(table,)).fetchall()
    for ref in refs:
        if ref['field'] not in body or body[ref['field']] is None:continue
        if ref['schema']!='data' or ref['count']!=1 or not allowed(user,ref['target'],'read'):
            raise HTTPException(403,'Referenced record is not accessible')
        scope,params=constraint(conn,ref['target'],user)
        row=conn.execute(sql.SQL('SELECT 1 FROM data.{} WHERE {}=%s AND {}').format(sql.Identifier(ref['target']),sql.Identifier(ref['target_field']),scope),[body[ref['field']]]+params).fetchone()
        if not row:raise HTTPException(403,'Referenced record is not accessible')
