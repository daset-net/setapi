"""Account lifecycle; secrets stay server-side and one-time credentials are hashed."""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import struct
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, ConfigDict
from psycopg.types.json import Jsonb
from . import db, storage
from .organizations import require_active
from .security import principal, admin, issue, audit, rate_limit, verify_password, hash_password, DUMMY_HASH, validate_scopes

router = APIRouter(prefix='/api', tags=['Accounts and security'])


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def totp(secret, step):
    key = base64.b32decode(secret)
    value = hmac.new(key, struct.pack('>Q',step), hashlib.sha1).digest()
    offset = value[-1]&15
    return str((struct.unpack('>I',value[offset:offset+4])[0]&0x7fffffff)%1000000).zfill(6)


def verify_otp(conn, user, code):
    if not user['mfa_secret']:
        return
    secret = storage.decrypt(user['mfa_secret'])['secret']
    step = int(time.time()//30)
    for candidate in (step-1,step,step+1):
        if candidate>user['mfa_step'] and hmac.compare_digest(totp(secret,candidate),code):
            conn.execute('UPDATE setapi.users SET mfa_step=%s WHERE id=%s',(candidate,user['id']))
            return
    hashed = digest(code)
    recovery = list(user['recovery_hashes'])
    if hashed in recovery:
        recovery.remove(hashed)
        conn.execute('UPDATE setapi.users SET recovery_hashes=%s WHERE id=%s',(Jsonb(recovery),user['id']))
        return
    raise HTTPException(401,'Invalid credentials or verification code')


def attempt_limit(request, email):
    rate_limit('setapi:login:ip:'+request.client.host,200,300)
    rate_limit('setapi:login:email:'+digest(email.lower()),20,300)


def login_user(body, request, audience):
    attempt_limit(request,body.email)
    if audience=='panel' and request.headers.get('origin') not in (None,settings().public_url):
        raise HTTPException(403,'Invalid origin')
    with db.connection() as conn:
        user=conn.execute('SELECT * FROM setapi.users WHERE email=%s AND audience=%s',(body.email.lower(),audience)).fetchone()
    valid=verify_password(user['password_hash'] if user else DUMMY_HASH,body.password)
    if not valid or not user or not user['active']:
        raise HTTPException(401,'Invalid credentials or verification code')
    with db.connection() as conn:
        current=conn.execute('SELECT * FROM setapi.users WHERE id=%s FOR UPDATE',(user['id'],)).fetchone()
        if not current['active'] or current['password_hash']!=user['password_hash']:
            raise HTTPException(401,'Invalid credentials')
        if current['tenant_id'] is not None and not conn.execute('SELECT 1 FROM setapi.organizations WHERE id=%s AND active',(current['tenant_id'],)).fetchone():
            raise HTTPException(401,'Invalid credentials or organization unavailable')
        verify_otp(conn,current,body.otp)
        token=issue(conn,user['id'],'App session' if audience=='app' else 'Panel session','session',current['scopes'],audience=='panel' and current['role']=='admin',12)
        audit(conn,user,'auth.login',audience)
    return current,token


from .config import settings


class Credentials(BaseModel):
    model_config=ConfigDict(extra='forbid')
    email: str=Field(min_length=3,max_length=254)
    password: str=Field(min_length=1,max_length=1024)
    otp: str=Field(default='',max_length=100)


@router.post('/app-auth/login')
def app_login(body:Credentials,request:Request):
    user,token=login_user(body,request,'app')
    return {'token':token['token'],'expires_at':token['expires_at'],'user':{'id':user['id'],'email':user['email'],'tenant_id':user['tenant_id']}}


class PasswordChange(BaseModel):
    current_password: str=Field(max_length=1024)
    password: str=Field(min_length=12,max_length=1024)
    otp: str=Field(default='',max_length=100)


def reauthenticate(user,password):
    rate_limit('setapi:reauth:'+str(user['id']),10,300)
    with db.connection() as conn:
        row=conn.execute('SELECT * FROM setapi.users WHERE id=%s',(user['id'],)).fetchone()
    if not verify_password(row['password_hash'],password):
        raise HTTPException(401,'Invalid credentials')
    return row


@router.post('/auth/password')
def change_password(body:PasswordChange,response:Response,user=Depends(principal)):
    old=reauthenticate(user,body.current_password)
    encoded=hash_password(body.password)
    with db.connection() as conn:
        current=conn.execute('SELECT * FROM setapi.users WHERE id=%s FOR UPDATE',(user['id'],)).fetchone()
        if current['password_hash']!=old['password_hash']:
            raise HTTPException(409,'Credentials changed; sign in again')
        verify_otp(conn,current,body.otp)
        conn.execute('UPDATE setapi.users SET password_hash=%s WHERE id=%s',(encoded,user['id']))
        conn.execute('UPDATE setapi.tokens SET revoked_at=now() WHERE user_id=%s',(user['id'],))
        conn.execute('DELETE FROM setapi.action_tokens WHERE user_id=%s',(user['id'],))
        audit(conn,user,'auth.password','account')
    response.delete_cookie('setapi_session')
    return {'ok':True,'login_required':True}


@router.get('/auth/sessions')
def sessions(user=Depends(principal)):
    with db.connection() as conn:
        return {'data':conn.execute("SELECT id,name,created_at,expires_at FROM setapi.tokens WHERE user_id=%s AND kind='session' AND revoked_at IS NULL AND expires_at>now() ORDER BY created_at DESC LIMIT 100",(user['id'],)).fetchall()}


@router.delete('/auth/sessions/{session_id}',status_code=204)
def revoke_session(session_id:UUID,user=Depends(principal)):
    with db.connection() as conn:
        conn.execute("UPDATE setapi.tokens SET revoked_at=now() WHERE id=%s AND user_id=%s AND kind='session'",(session_id,user['id']))


class Reauth(BaseModel):
    password: str=Field(max_length=1024)
    otp: str=Field(default='',max_length=100)


@router.post('/auth/mfa/setup')
def mfa_setup(body:Reauth,user=Depends(principal)):
    row=reauthenticate(user,body.password)
    if row['mfa_secret']:
        raise HTTPException(409,'MFA is already enabled')
    secret=base64.b32encode(secrets.token_bytes(20)).decode()
    db.cache.set('setapi:mfa:'+str(user['token_id']),storage.encrypt({'secret':secret,'password_hash':row['password_hash']}),ex=600)
    return {'secret':secret,'uri':'otpauth://totp/SETAPI?'+urlencode({'secret':secret,'issuer':'SETAPI'})}


@router.post('/auth/mfa/enable')
def mfa_enable(body:Reauth,user=Depends(principal)):
    row=reauthenticate(user,body.password)
    raw=db.cache.get('setapi:mfa:'+str(user['token_id']))
    if not raw:
        raise HTTPException(400,'Enrollment expired')
    data=storage.decrypt(raw);secret=data['secret'];step=int(time.time()//30)
    if data['password_hash']!=row['password_hash'] or not hmac.compare_digest(totp(secret,step),body.otp):
        raise HTTPException(401,'Invalid verification code')
    recovery=[secrets.token_hex(10) for _ in range(10)]
    with db.connection() as conn:
        current=conn.execute('SELECT * FROM setapi.users WHERE id=%s FOR UPDATE',(user['id'],)).fetchone()
        if current['mfa_secret'] or current['password_hash']!=row['password_hash']:
            raise HTTPException(409,'Account changed; restart enrollment')
        conn.execute('UPDATE setapi.users SET mfa_secret=%s,mfa_step=%s,recovery_hashes=%s WHERE id=%s',(storage.encrypt({'secret':secret}),step,Jsonb([digest(c) for c in recovery]),user['id']))
        conn.execute('UPDATE setapi.tokens SET revoked_at=now() WHERE user_id=%s AND id<>%s',(user['id'],user['token_id']))
        audit(conn,user,'auth.mfa.enable','account')
    db.cache.delete('setapi:mfa:'+str(user['token_id']))
    return {'recovery_codes':recovery}


@router.post('/auth/mfa/disable')
def mfa_disable(body:Reauth,user=Depends(principal)):
    row=reauthenticate(user,body.password)
    with db.connection() as conn:
        current=conn.execute('SELECT * FROM setapi.users WHERE id=%s FOR UPDATE',(user['id'],)).fetchone()
        if current['password_hash']!=row['password_hash']:
            raise HTTPException(409,'Account changed')
        verify_otp(conn,current,body.otp)
        conn.execute("UPDATE setapi.users SET mfa_secret=NULL,mfa_step=-1,recovery_hashes='[]' WHERE id=%s",(user['id'],))
        conn.execute('UPDATE setapi.tokens SET revoked_at=now() WHERE user_id=%s AND id<>%s',(user['id'],user['token_id']))
        audit(conn,user,'auth.mfa.disable','account')
    return {'ok':True}


def queue_email(conn,user,purpose):
    token=secrets.token_urlsafe(40)
    conn.execute('DELETE FROM setapi.action_tokens WHERE user_id=%s AND purpose=%s',(user['id'],purpose))
    conn.execute('INSERT INTO setapi.action_tokens VALUES(%s,%s,%s,%s)',(digest(token),user['id'],purpose,datetime.now(timezone.utc)+timedelta(minutes=30)))
    link=settings().public_url+'/#'+urlencode({'action':purpose,'token':token})
    conn.execute('INSERT INTO setapi.mail_queue(payload_encrypted) VALUES(%s)',(storage.encrypt({'to':user['email'],'subject':'SETAPI — '+('Confirme seu e-mail' if purpose=='verify' else 'Redefina sua senha'),'text':'Abra este link em até 30 minutos:\n'+link+'\nSe não solicitou, ignore esta mensagem.'}),))


class EmailRequest(BaseModel):
    email: str=Field(min_length=3,max_length=254)


def mail_ready():
    return bool(os.getenv('SETAPI_SMTP_HOST') and os.getenv('SETAPI_SMTP_FROM'))


@router.post('/auth/forgot-password',status_code=202)
def forgot(body:EmailRequest,request:Request):
    started=time.monotonic()
    if not mail_ready():
        raise HTTPException(503,'Password recovery is not configured')
    attempt_limit(request,body.email)
    with db.connection() as conn:
        user=conn.execute('SELECT * FROM setapi.users WHERE email=%s AND active',(body.email.lower(),)).fetchone()
        if user:queue_email(conn,user,'reset')
    time.sleep(max(0, .25+secrets.randbelow(100)/1000-(time.monotonic()-started)))
    return {'message':'Se a conta estiver cadastrada, você receberá as instruções.'}


class Reset(BaseModel):
    token:str=Field(min_length=20,max_length=128)
    password:str=Field(min_length=12,max_length=1024)
    otp:str=Field(default='',max_length=100)


@router.post('/auth/reset-password')
def reset(body:Reset,request:Request):
    rate_limit('setapi:reset:ip:'+request.client.host,20,300)
    encoded=hash_password(body.password)
    with db.connection() as conn:
        action=conn.execute("SELECT * FROM setapi.action_tokens WHERE digest=%s AND purpose='reset' AND expires_at>now() FOR UPDATE",(digest(body.token),)).fetchone()
        if not action:raise HTTPException(400,'Invalid or expired link')
        user=conn.execute('SELECT * FROM setapi.users WHERE id=%s AND active FOR UPDATE',(action['user_id'],)).fetchone()
        if not user:raise HTTPException(400,'Invalid or expired link')
        verify_otp(conn,user,body.otp)
        conn.execute('UPDATE setapi.users SET password_hash=%s WHERE id=%s',(encoded,user['id']))
        conn.execute('DELETE FROM setapi.action_tokens WHERE user_id=%s',(user['id'],))
        conn.execute('UPDATE setapi.tokens SET revoked_at=now() WHERE user_id=%s',(user['id'],))
        audit(conn,user,'auth.reset','account')
    return {'ok':True}


@router.post('/app-auth/register',status_code=202)
def register(body:Credentials,request:Request):
    if os.getenv('SETAPI_ALLOW_REGISTRATION','false')!='true' or not mail_ready():
        raise HTTPException(403,'Public registration is disabled')
    if len(body.password)<12 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',body.email):
        raise HTTPException(422,'Provide a valid email and password with at least 12 characters')
    attempt_limit(request,body.email)
    encoded=hash_password(body.password)
    with db.connection() as conn:
        # No scopes, tenant or role can be supplied by public registration.
        user=conn.execute("INSERT INTO setapi.users(email,password_hash,role,audience,active) VALUES(%s,%s,'member','app',false) ON CONFLICT(email) DO NOTHING RETURNING *",(body.email.lower(),encoded)).fetchone()
        if user:queue_email(conn,user,'verify')
    return {'message':'Se o cadastro puder ser criado, você receberá um e-mail de confirmação.'}


class ActionToken(BaseModel):
    token:str=Field(min_length=20,max_length=128)


@router.post('/app-auth/verify')
def verify_email(body:ActionToken,request:Request):
    rate_limit('setapi:verify:ip:'+request.client.host,30,300)
    with db.connection() as conn:
        row=conn.execute("DELETE FROM setapi.action_tokens WHERE digest=%s AND purpose='verify' AND expires_at>now() RETURNING user_id",(digest(body.token),)).fetchone()
        if not row:raise HTTPException(400,'Invalid or expired link')
        conn.execute("UPDATE setapi.users SET active=true WHERE id=%s AND audience='app'",(row['user_id'],))
    return {'ok':True}


class Access(BaseModel):
    scopes:dict[str,list[str]]=Field(default_factory=dict)
    tenant_id:UUID|None=None


@router.put('/users/{user_id}/access')
def access(user_id:UUID,body:Access,user=Depends(admin)):
    validate_scopes(body.scopes)
    with db.connection() as conn:
        require_active(conn,body.tenant_id)
        existing=conn.execute('SELECT role FROM setapi.users WHERE id=%s',(user_id,)).fetchone()
        if existing and existing['role']=='admin' and body.tenant_id is not None:
            raise HTTPException(422,'Global administrators cannot be organization members')
        row=conn.execute('UPDATE setapi.users SET scopes=%s,tenant_id=%s WHERE id=%s RETURNING id',(Jsonb(body.scopes),body.tenant_id,user_id)).fetchone()
        if not row:raise HTTPException(404,'User not found')
        conn.execute('UPDATE setapi.tokens SET revoked_at=now() WHERE user_id=%s',(user_id,))
        audit(conn,user,'user.access',str(user_id))
    return {'ok':True}
