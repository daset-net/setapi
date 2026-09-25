import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from psycopg import errors, Error as PGError
from redis.exceptions import RedisError
from redis.asyncio import Redis
from . import db
from .config import settings
from .security import authenticate, allowed
from .admin_api import router as admin_router
from .data_api import router as data_router
from .storage_api import router as storage_router


@asynccontextmanager
async def lifespan(app):
    await asyncio.to_thread(db.start)
    yield
    await asyncio.to_thread(db.stop)


app = FastAPI(title='SETAPI', version='0.1.0', description='Self-hosted PostgreSQL API, realtime and storage.', lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings().cors_origins, allow_credentials=False,
                   allow_methods=['GET','POST','PUT','PATCH','DELETE'], allow_headers=['Authorization','Content-Type'])


class BodyLimitMiddleware:
    """Bound streamed bodies too, including requests without Content-Length."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        upload = scope['path'].startswith('/api/files/') and scope['method'] == 'POST'
        limit = (settings().max_upload_mb + 1) * 1024 * 1024 if upload else 1024 * 1024
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
    if request.method in ('POST', 'PUT', 'PATCH'):
        length = request.headers.get('content-length')
        if length:
            try:
                size = int(length)
            except ValueError:
                return JSONResponse({'detail': 'Invalid Content-Length'}, status_code=400)
            limit = (settings().max_upload_mb + 1) * 1024 * 1024 if request.url.path.startswith('/api/files/') else 1024 * 1024
            if size < 0:
                return JSONResponse({'detail': 'Invalid Content-Length'}, status_code=400)
            if size > limit:
                return JSONResponse({'detail': 'Request exceeds upload limit'}, status_code=413)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Frame-Options'] = 'DENY'
    if request.url.path.startswith('/api'):
        response.headers['Cache-Control'] = 'no-store'
    if request.url.path == '/':
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    return response


@app.exception_handler(PGError)
async def database_error(request, exc):
    if isinstance(exc, (errors.UniqueViolation, errors.ForeignKeyViolation, errors.DependentObjectsStillExist, errors.DuplicateTable, errors.DuplicateColumn)):
        return JSONResponse({'detail': 'Conflict with an existing record, name or relationship'}, status_code=409)
    if isinstance(exc, (errors.DataError, errors.NotNullViolation, errors.CheckViolation)):
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
    return {'status': 'ready'}


app.include_router(admin_router)
app.include_router(data_router)
app.include_router(storage_router)


@app.websocket('/ws')
async def websocket(ws: WebSocket):
    origin = ws.headers.get('origin')
    if origin and origin not in [settings().public_url, *settings().cors_origins]:
        await ws.close(code=1008)
        return
    await ws.accept()
    client, subscription, receiver = None, None, None
    try:
        hello = await asyncio.wait_for(ws.receive_json(), timeout=5)
        raw = hello.get('token')
        if not raw and origin == settings().public_url:
            raw = ws.cookies.get('setapi_session')
        user = await asyncio.to_thread(authenticate, raw)
        requested = hello.get('tables', [])
        if not isinstance(requested, list) or len(requested) > 100 or not all(isinstance(t, str) for t in requested):
            raise HTTPException(422, 'Invalid subscriptions')
        if any(not allowed(user, table, 'read') for table in requested):
            raise HTTPException(403, 'Subscription denied')
        client = Redis.from_url(settings().redis_url, decode_responses=True)
        subscription = client.pubsub()
        await subscription.subscribe('setapi:events')
        await ws.send_json({'type': 'ready', 'tables': requested, 'resync': True})
        receiver = asyncio.create_task(ws.receive())
        elapsed = 0
        while True:
            if receiver.done():
                frame = receiver.result()
                if frame['type'] == 'websocket.disconnect':
                    break
                receiver = asyncio.create_task(ws.receive())
            message = await subscription.get_message(ignore_subscribe_messages=True, timeout=1)
            elapsed += 1
            if message:
                user = await asyncio.to_thread(authenticate, raw)
                event = json.loads(message['data'])
                if event['table'] in requested and allowed(user, event['table'], 'read'):
                    await ws.send_json({'type': 'change', **event})
            if elapsed >= 10:
                await asyncio.to_thread(authenticate, raw)
                await ws.send_json({'type': 'ping'})
                elapsed = 0
    except (HTTPException, asyncio.TimeoutError, ValueError, TypeError, AttributeError):
        await ws.close(code=1008)
    except (WebSocketDisconnect, RuntimeError):
        pass
    except RedisError:
        await ws.close(code=1013)
    finally:
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
