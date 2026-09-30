import asyncio
import json
import logging
import httpx
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from psycopg import errors, Error as PGError
from redis.exceptions import RedisError
from redis.asyncio import Redis
from uuid import UUID
from . import db, tables
from .config import settings
from .security import authenticate, allowed, rate_limit
import os
from collections import Counter
ws_counts=Counter()
from .admin_api import router as admin_router
from .data_api import router as data_router
from .storage_api import router as storage_router
from .google_oauth import router as google_router
from .mail_api import router as mail_router
from .mcp import router as mcp_router
from .accounts import router as accounts_router
from .organizations import router as organizations_router
from .policies import router as policies_router
from .platform_settings import router as platform_router
from .cloudflare_r2 import router as cloudflare_router
from .export import router as export_router
from . import platform_settings
from . import policies
from fastapi.exceptions import RequestValidationError
from fastapi import Depends
from .security import admin
from psycopg_pool import PoolTimeout, TooManyRequests


@asynccontextmanager
async def lifespan(app):
    await asyncio.to_thread(db.start)
    yield
    await asyncio.to_thread(db.stop)


app = FastAPI(title='SETAPI', version='0.3.0', description='Self-hosted PostgreSQL API, realtime and storage.', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(CORSMiddleware, allow_origins=settings().cors_origins, allow_credentials=False,
                   allow_methods=['GET','POST','PUT','PATCH','DELETE'], allow_headers=['Authorization','Content-Type'])


def upload_limit(path):
    # MCP carries files as base64 inside JSON, about 4/3 of their size.
    megabytes = platform_settings.current()['max_upload_mb'] + 1
    return (megabytes * 4 // 3 + 1 if path == '/mcp' else megabytes) * 1024 * 1024


class BodyLimitMiddleware:
    """Bound streamed bodies too, including requests without Content-Length."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        upload = scope['path'].startswith('/api/files/') and scope['method'] == 'POST'
        limit = upload_limit(scope['path']) if upload or scope['path'] == '/mcp' else 1024 * 1024
        consumed = 0

        async def bounded_receive():
            nonlocal consumed
            message = await receive()
            if message['type'] == 'http.request':
                consumed += len(message.get('body', b''))
                if consumed > limit:
                    raise HTTPException(413, 'Request exceeds size limit')
            return message

        return await self.app(scope, bounded_receive, send)


app.add_middleware(BodyLimitMiddleware)


@app.middleware('http')
async def headers(request: Request, call_next):
    if request.url.path.startswith('/api/'):
        try:
            await asyncio.to_thread(rate_limit,'setapi:http:ip:'+request.client.host,10000)
        except HTTPException as exc:
            return JSONResponse({'detail':exc.detail},status_code=exc.status_code,headers=exc.headers)
        except RedisError:
            return JSONResponse({'detail':'Service temporarily unavailable'},status_code=503)
    if request.method in ('POST', 'PUT', 'PATCH'):
        length = request.headers.get('content-length')
        if length:
            try:
                size = int(length)
            except ValueError:
                return JSONResponse({'detail': 'Invalid Content-Length'}, status_code=400)
            limit = upload_limit(request.url.path) if request.url.path.startswith('/api/files/') or request.url.path == '/mcp' else 1024 * 1024
            if size < 0:
                return JSONResponse({'detail': 'Invalid Content-Length'}, status_code=400)
            if size > limit:
                return JSONResponse({'detail': 'Request exceeds upload limit'}, status_code=413)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Frame-Options'] = 'DENY'
    if settings().cookie_secure:
        response.headers['Strict-Transport-Security']='max-age=31536000'
    response.headers['Permissions-Policy']='camera=(), microphone=(), geolocation=()'
    if request.url.path.startswith('/api') or request.url.path in ('/docs','/openapi.json'):
        response.headers['Cache-Control'] = 'no-store'
    if request.url.path == '/':
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    return response


@app.exception_handler(PGError)
async def database_error(request, exc):
    if isinstance(exc, (errors.UniqueViolation, errors.ForeignKeyViolation, errors.DependentObjectsStillExist, errors.DuplicateTable, errors.DuplicateColumn)):
        return JSONResponse({'detail': 'Conflict with an existing record, name or relationship'}, status_code=409)
    if isinstance(exc, (errors.DataError, errors.NotNullViolation, errors.CheckViolation, errors.CannotCoerce, errors.DatatypeMismatch)):
        return JSONResponse({'detail': 'Invalid value, type or required field'}, status_code=422)
    if isinstance(exc, errors.UndefinedColumn):
        return JSONResponse({'detail': 'Column not found'}, status_code=404)
    logging.error('Database operation failed (%s)', type(exc).__name__)
    return JSONResponse({'detail': 'Database operation failed'}, status_code=503)


@app.exception_handler(RedisError)
async def redis_error(request, exc):
    return JSONResponse({'detail': 'Realtime service unavailable'}, status_code=503)


@app.get('/health/live', include_in_schema=False)
def live():
    return {'status': 'ok'}


@app.get('/health/ready', include_in_schema=False)
def ready():
    with db.connection() as conn:
        conn.execute('SELECT 1')
    db.cache.ping()
    from . import postgrest
    if postgrest.enabled():
        try:
            response = postgrest.HTTP.get('http://127.0.0.1:3001/ready', timeout=2)
            if response.status_code != 200:
                raise HTTPException(503, 'Data engine unavailable')
        except httpx.HTTPError:
            raise HTTPException(503, 'Data engine unavailable') from None
    return {'status': 'ready'}


app.include_router(admin_router)
app.include_router(data_router)
app.include_router(storage_router)
app.include_router(google_router)
app.include_router(mail_router)
app.include_router(mcp_router)
app.include_router(accounts_router)
app.include_router(organizations_router)
app.include_router(policies_router)
app.include_router(platform_router)
app.include_router(cloudflare_router)
app.include_router(export_router)


@app.websocket('/ws')
async def websocket(ws: WebSocket):
    origin = ws.headers.get('origin')
    if origin and origin not in [settings().public_url, *settings().cors_origins]:
        await ws.close(code=1008)
        return
    if sum(ws_counts.values())>=int(os.getenv('SETAPI_WS_MAX','1000')):
        await ws.close(code=1013);return
    await ws.accept()
    client, subscription, receiver = None, None, None
    counted=None
    try:
        await asyncio.to_thread(rate_limit,'setapi:ws:ip:'+ws.client.host,1000)
        hello = await asyncio.wait_for(ws.receive_json(), timeout=5)
        raw = hello.get('token')
        if not raw and origin == settings().public_url:
            raw = ws.cookies.get('setapi_session')
        user = await asyncio.to_thread(authenticate, raw)
        if ws_counts[user['id']]>=10:
            raise HTTPException(429,'Too many connections')
        ws_counts[user['id']]+=1;counted=user['id']
        requested = hello.get('tables', [])
        if not isinstance(requested, list) or len(requested) > 100 or not all(isinstance(t, str) for t in requested):
            raise HTTPException(422, 'Invalid subscriptions')
        organization = UUID(hello['organization_id']) if hello.get('organization_id') else None
        def resolve():
            with db.connection() as conn:
                return {tables.target(conn, user, t, organization): t for t in requested}
        # Stored name -> API name: events carry the stored one, clients only ever see the API one.
        names = await asyncio.to_thread(resolve)
        if any(not allowed(user, name, 'read', physical) for physical, name in names.items()):
            raise HTTPException(403, 'Subscription denied')
        stored = list(names)
        routing=await asyncio.to_thread(policies.routing_rules,user,stored)
        client = Redis.from_url(settings().redis_url, decode_responses=True)
        subscription = client.pubsub()
        channels=policies.channels(user,stored,routing)
        await subscription.subscribe(*channels)
        await ws.send_json({'type': 'ready', 'tables': requested, 'resync': True})
        receiver = asyncio.create_task(ws.receive())
        last_check=asyncio.get_running_loop().time()
        while True:
            if receiver.done():
                frame = receiver.result()
                if frame['type'] == 'websocket.disconnect':
                    break
                receiver = asyncio.create_task(ws.receive())
            message = await subscription.get_message(ignore_subscribe_messages=True, timeout=1)
            if message:
                event = json.loads(message['data'])
                if event['table'] in names and not policies.skip_event(user,event,routing):
                    user = await asyncio.to_thread(authenticate, raw)
                    if allowed(user, names[event['table']], 'read', event['table']) and await asyncio.to_thread(policies.visible_event,user,event):
                        await asyncio.wait_for(ws.send_json({'type':'change', **{k:v for k,v in event.items() if k in ('id','operation','event_id')}, 'table':names[event['table']]}),timeout=5)
            if asyncio.get_running_loop().time()-last_check>=10:
                user=await asyncio.to_thread(authenticate, raw)
                if any(not allowed(user,name,'read',physical) for physical,name in names.items()):raise HTTPException(403,'Subscription revoked')
                routing=await asyncio.to_thread(policies.routing_rules,user,stored)
                updated=policies.channels(user,stored,routing)
                if updated!=channels:
                    await subscription.unsubscribe(*channels)
                    await subscription.subscribe(*updated)
                    channels=updated
                    await asyncio.wait_for(ws.send_json({'type':'ready','tables':requested,'resync':True}),timeout=5)
                await asyncio.wait_for(ws.send_json({'type': 'ping'}),timeout=5)
                last_check=asyncio.get_running_loop().time()

    except (HTTPException, asyncio.TimeoutError, ValueError, TypeError, AttributeError):
        await ws.close(code=1008)
    except (WebSocketDisconnect, RuntimeError):
        pass
    except RedisError:
        await ws.close(code=1013)
    finally:
        if counted:
            ws_counts[counted]-=1
            if not ws_counts[counted]:del ws_counts[counted]
        if receiver and not receiver.done():
            receiver.cancel()
        if subscription:
            await subscription.aclose()
        if client:
            await client.aclose()


STATIC = Path(__file__).parent / 'static'
app.mount('/static', StaticFiles(directory=STATIC), name='static')


@app.get('/', include_in_schema=False)
def panel():
    return FileResponse(STATIC / 'index.html')


@app.exception_handler(RequestValidationError)
async def invalid_request(request,exc):
    # Pydantic's default error includes input values, potentially passwords/tokens.
    return JSONResponse({'detail':'Invalid request fields','errors':[{'loc':e['loc'],'type':e['type']} for e in exc.errors()]},status_code=422)


@app.exception_handler(PoolTimeout)
@app.exception_handler(TooManyRequests)
async def database_busy(request,exc):
    return JSONResponse({'detail':'Service busy; retry shortly'},status_code=503,headers={'Retry-After':'2'})


@app.get('/openapi.json',include_in_schema=False)
def openapi_schema(user=Depends(admin)):
    return app.openapi()


@app.get('/docs',include_in_schema=False)
def api_docs(user=Depends(admin)):
    from fastapi.openapi.docs import get_swagger_ui_html
    return get_swagger_ui_html(openapi_url='/openapi.json',title='SETAPI API')
