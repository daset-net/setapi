"""MCP server (Streamable HTTP, stateless) that mirrors the REST API one tool per operation.

Tools are generated from the OpenAPI document and every call is replayed through the REST API itself,
with the caller's own token, so validation, permissions, rate limits and audit are exactly the REST ones.
"""
import asyncio
import base64
import json
import re
from urllib.parse import quote
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.requests import ClientDisconnect
from .config import settings
from .security import authenticate

router = APIRouter(include_in_schema=False)
VERSIONS = ('2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05')
# Friendlier names where the route function name alone is vague.
NAMES = {'users': 'list_users', 'tokens': 'list_tokens', 'me': 'get_me', 'status': 'get_status',
         'indexes': 'list_indexes', 'audit_log': 'list_audit', 'toggle_user': 'update_user',
         'access': 'set_user_access', 'forgot': 'forgot_password', 'reset': 'reset_password',
         'sessions': 'list_sessions'}
INSTRUCTIONS = ('SETAPI: backend REST sobre PostgreSQL. Cada ferramenta corresponde a uma rota da API REST e responde '
                'exatamente como ela, com {"status", "body"}. Parâmetros de caminho e de consulta vão no nível de cima; '
                'o corpo JSON da rota vai em "body". Estrutura: create_table, add_column, edit_column, rename_column, '
                'drop_column, drop_table. Registros: list_records, get_record, create_record, update_record, delete_record.')
_tools = None


def resolve(schema, components, seen=()):
    """Inline $ref so each tool schema stands alone, as MCP clients expect."""
    if isinstance(schema, list):
        return [resolve(s, components, seen) for s in schema]
    if not isinstance(schema, dict):
        return schema
    if '$ref' in schema:
        name = schema['$ref'].rsplit('/', 1)[-1]
        if name in seen:
            return {'type': 'object'}
        return resolve(components[name], components, seen + (name,))
    return {k: resolve(v, components, seen) for k, v in schema.items()}


def function_name(op, path, method):
    # FastAPI builds operationId as function name + path + method, with non-word characters as "_".
    operation, suffix = op.get('operationId', ''), re.sub(r'\W', '_', path) + '_' + method
    return operation[:-len(suffix)] if operation.endswith(suffix) and len(operation) > len(suffix) else operation


AREAS = {'/api/integrations/google/': 'google_', '/api/mail/': 'mail_'}


def tool_name(function, path, taken):
    name = NAMES.get(function, function)
    area = next((prefix for start, prefix in AREAS.items() if path.startswith(start)), '')
    if area and not name.startswith(area):
        name = area + name
    if name in taken:
        parts = [p for p in path.split('/')[2:] if p and not p.startswith('{')]
        name = (parts[-2] if len(parts) > 1 else parts[0]).replace('-', '_') + '_' + name
    return re.sub(r'[^A-Za-z0-9_-]', '_', name)[:64]


def build(app):
    spec = app.openapi()
    components = spec.get('components', {}).get('schemas', {})
    tools, taken = {}, set()
    for path, operations in spec['paths'].items():
        for method, op in operations.items():
            name = tool_name(function_name(op, path, method), path, taken)
            taken.add(name)
            properties, required, params = {}, [], []
            for p in op.get('parameters', []):
                if p['in'] not in ('path', 'query'):
                    continue
                schema = resolve(p.get('schema', {}), components)
                if p.get('description'):
                    schema = dict(schema, description=p['description'])
                properties[p['name']] = schema
                params.append((p['name'], p['in']))
                if p.get('required'):
                    required.append(p['name'])
            body = op.get('requestBody')
            form = None
            if body:
                kind, content = next(iter(body['content'].items()))
                schema = resolve(content['schema'], components)
                if kind == 'multipart/form-data':
                    # Binary form fields travel as base64 inside JSON.
                    form = [k for k, v in schema.get('properties', {}).items() if v.get('format') == 'binary' or v.get('contentMediaType')]
                    for field in form:
                        schema['properties'][field] = {'type': 'object', 'required': ['filename', 'content_base64'], 'properties': {
                            'filename': {'type': 'string'}, 'content_base64': {'type': 'string', 'description': 'Conteúdo do arquivo em base64'},
                            'content_type': {'type': 'string', 'description': 'Tipo MIME, por exemplo application/pdf'}}}
                properties['body'] = schema
                if body.get('required'):
                    required.append('body')
            summary = (op.get('description') or '').strip() or op.get('summary') or name
            tools[name] = {
                'method': method.upper(), 'path': path, 'params': params, 'form': form,
                'definition': {
                    'name': name, 'title': op.get('summary') or name,
                    'description': f'{summary} ({method.upper()} {path})',
                    'inputSchema': {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False},
                    'annotations': {'readOnlyHint': method == 'get', 'destructiveHint': method == 'delete',
                                    'idempotentHint': method in ('get', 'put', 'delete'), 'openWorldHint': False}}}
    return tools


def tools_for(app):
    global _tools
    if _tools is None:
        _tools = build(app)
    return _tools


def rpc_error(request_id, code, message):
    return {'jsonrpc': '2.0', 'id': request_id, 'error': {'code': code, 'message': message}}


def result_of(response):
    kind = response.headers.get('content-type', '')
    if response.status_code == 204 or not response.content:
        body = None
    elif kind.startswith('application/json'):
        body = response.json()
    elif kind.startswith('text/'):
        body = response.text
    else:
        disposition = response.headers.get('content-disposition', '')
        match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', disposition)
        body = {'content_type': kind, 'filename': match.group(1) if match else None, 'size': len(response.content),
                'content_base64': base64.b64encode(response.content).decode()}
    payload = {'status': response.status_code, 'body': body}
    result = {'content': [{'type': 'text', 'text': json.dumps(payload, ensure_ascii=False, default=str)}],
              'structuredContent': payload, 'isError': response.status_code >= 400}
    return result


async def call_tool(app, request, authorization, tool, arguments):
    if not isinstance(arguments, dict):
        raise ValueError('arguments must be an object')
    unknown = set(arguments) - set(tool['definition']['inputSchema']['properties'])
    if unknown:
        raise ValueError('Unknown arguments: ' + ', '.join(sorted(unknown)))
    path, query = tool['path'], {}
    for name, where in tool['params']:
        if name not in arguments:
            continue
        value = arguments[name]
        if where == 'path':
            path = path.replace('{' + name + '}', quote(str(value), safe=''))
        else:
            query[name] = json.dumps(value) if isinstance(value, (dict, list)) else str(value).lower() if isinstance(value, bool) else value
    if '{' in path:
        raise ValueError('Missing path parameter: ' + re.search(r'\{(\w+)\}', path).group(1))
    options = {'params': query, 'headers': {'Authorization': authorization}}
    body = arguments.get('body')
    if tool['form'] is not None and body is not None:
        files, data = {}, {}
        for key, value in body.items():
            if key in tool['form']:
                files[key] = (value['filename'], base64.b64decode(value['content_base64'], validate=True),
                              value.get('content_type') or 'application/octet-stream')
            else:
                data[key] = value
        options.update(files=files, data=data)
    elif body is not None:
        options['json'] = body
    # Replay through the REST app itself, keeping the caller's address for per-IP limits.
    transport = httpx.ASGITransport(app=app, client=(request.client.host if request.client else '127.0.0.1', 0))
    async with httpx.AsyncClient(transport=transport, base_url=settings().public_url, timeout=None) as client:
        response = await client.request(tool['method'], path, **options)
    return result_of(response)


async def handle(app, request, authorization, message):
    if not isinstance(message, dict) or message.get('jsonrpc') != '2.0' or not isinstance(message.get('method'), str):
        return rpc_error(message.get('id') if isinstance(message, dict) else None, -32600, 'Invalid Request')
    request_id, method, params = message.get('id'), message['method'], message.get('params') or {}
    if 'id' not in message:
        return None  # notification, e.g. notifications/initialized
    if method == 'initialize':
        asked = params.get('protocolVersion')
        return {'jsonrpc': '2.0', 'id': request_id, 'result': {
            'protocolVersion': asked if asked in VERSIONS else VERSIONS[0],
            'capabilities': {'tools': {'listChanged': False}},
            'serverInfo': {'name': 'setapi', 'title': 'SETAPI', 'version': app.version},
            'instructions': INSTRUCTIONS}}
    if method == 'ping':
        return {'jsonrpc': '2.0', 'id': request_id, 'result': {}}
    if method == 'tools/list':
        return {'jsonrpc': '2.0', 'id': request_id, 'result': {'tools': [t['definition'] for t in tools_for(app).values()]}}
    if method == 'tools/call':
        tool = tools_for(app).get(params.get('name'))
        if not tool:
            return rpc_error(request_id, -32602, 'Unknown tool: ' + str(params.get('name')))
        try:
            result = await call_tool(app, request, authorization, tool, params.get('arguments') or {})
        except (ValueError, KeyError, TypeError) as exc:
            result = {'content': [{'type': 'text', 'text': 'Argumentos inválidos: ' + str(exc)}], 'isError': True}
        return {'jsonrpc': '2.0', 'id': request_id, 'result': result}
    return rpc_error(request_id, -32601, 'Method not found: ' + method)


@router.post('/mcp')
async def mcp(request: Request):
    origin = request.headers.get('origin')
    if origin and origin != settings().public_url and origin not in settings().cors_origins:
        return JSONResponse({'detail': 'Origin not allowed'}, status_code=403)
    authorization = request.headers.get('authorization', '')
    try:
        if not authorization.startswith('Bearer '):
            raise ValueError
        await asyncio.to_thread(authenticate, authorization[7:])
    except Exception:
        return JSONResponse({'detail': 'Use Authorization: Bearer <token do SETAPI>'}, status_code=401,
                            headers={'WWW-Authenticate': 'Bearer realm="setapi"'})
    try:
        message = await request.json()
    except ValueError:
        return JSONResponse(rpc_error(None, -32700, 'Parse error'), status_code=400)
    except ClientDisconnect:
        return Response(status_code=400)
    if isinstance(message, list):
        answers = [a for a in [await handle(request.app, request, authorization, m) for m in message] if a]
        return JSONResponse(answers) if answers else Response(status_code=202)
    answer = await handle(request.app, request, authorization, message)
    return JSONResponse(answer) if answer else Response(status_code=202)


@router.get('/mcp')
@router.delete('/mcp')
def mcp_stream():
    # Stateless server: no server-initiated stream and no session to end.
    return Response(status_code=405, headers={'Allow': 'POST'})
