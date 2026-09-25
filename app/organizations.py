from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ConfigDict
from . import db
from .security import principal,admin,is_admin,audit

router=APIRouter(prefix='/api/organizations',tags=['Organizations'])


class Organization(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name:str=Field(min_length=1,max_length=120)


class OrganizationUpdate(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name:str|None=Field(default=None,min_length=1,max_length=120)
    active:bool|None=None


def require_active(conn,organization_id):
    if organization_id is not None and not conn.execute('SELECT 1 FROM setapi.organizations WHERE id=%s AND active',(organization_id,)).fetchone():
        raise HTTPException(422,'Choose an active organization')


@router.get('')
def list_organizations(user=Depends(principal)):
    with db.connection() as conn:
        if is_admin(user):
            rows=conn.execute('''SELECT o.id,o.name,o.active,o.created_at,
              (SELECT count(*) FROM setapi.users u WHERE u.tenant_id=o.id) AS users
              FROM setapi.organizations o ORDER BY o.name''').fetchall()
        else:
            rows=conn.execute('SELECT id,name,active,created_at FROM setapi.organizations WHERE id=%s AND active',(user.get('tenant_id'),)).fetchall()
    return {'data':rows}


@router.get('/{organization_id}')
def get_organization(organization_id:UUID,user=Depends(principal)):
    if not is_admin(user) and user.get('tenant_id')!=organization_id:
        raise HTTPException(404,'Organization not found')
    with db.connection() as conn:
        row=conn.execute('SELECT id,name,active,created_at FROM setapi.organizations WHERE id=%s',(organization_id,)).fetchone()
    if not row:raise HTTPException(404,'Organization not found')
    return row


@router.post('',status_code=201)
def create_organization(body:Organization,user=Depends(admin)):
    if not body.name.strip():raise HTTPException(422,'Organization name is required')
    with db.connection() as conn:
        row=conn.execute('INSERT INTO setapi.organizations(name) VALUES(%s) RETURNING id,name,active,created_at',(body.name.strip(),)).fetchone()
        audit(conn,user,'organization.create',str(row['id']))
    return row


@router.patch('/{organization_id}')
def update_organization(organization_id:UUID,body:OrganizationUpdate,user=Depends(admin)):
    if body.name is not None and not body.name.strip():raise HTTPException(422,'Organization name is required')
    with db.connection() as conn:
        row=conn.execute('UPDATE setapi.organizations SET name=COALESCE(%s,name),active=COALESCE(%s,active) WHERE id=%s RETURNING id,name,active',(body.name.strip() if body.name is not None else None,body.active,organization_id)).fetchone()
        if not row:raise HTTPException(404,'Organization not found')
        if body.active is False:
            conn.execute('UPDATE setapi.tokens SET revoked_at=now() WHERE user_id IN (SELECT id FROM setapi.users WHERE tenant_id=%s)',(organization_id,))
            conn.execute('DELETE FROM setapi.action_tokens WHERE user_id IN (SELECT id FROM setapi.users WHERE tenant_id=%s)',(organization_id,))
        audit(conn,user,'organization.update',str(organization_id),{'active':row['active']})
    return row
