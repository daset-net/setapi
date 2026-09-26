#!/usr/bin/env python3
"""Isolated local load test; never points at a deployed service or customer DB."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import resource
import secrets
import statistics
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
from uuid import uuid4
import httpx
import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb
from cryptography.fernet import Fernet
from websockets.asyncio.client import connect

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))


def percentile(values,fraction):
    return round(sorted(values)[min(len(values)-1,int(len(values)*fraction))]*1000,2) if values else None


async def scenario(tokens,clients,seconds,pace):
    http_lat=[];errors={};events=0;ws_errors=0
    async with httpx.AsyncClient(base_url='http://127.0.0.1:8058',timeout=20,limits=httpx.Limits(max_connections=clients+5,max_keepalive_connections=clients+5)) as api:
        sockets=[]
        async def listen(ws):
            nonlocal events,ws_errors
            try:
                async for raw in ws:
                    if json.loads(raw).get('type')=='change':events+=1
            except Exception:ws_errors+=1
        for token in tokens[:clients]:
            ws=await connect('ws://127.0.0.1:8058/ws',origin='http://127.0.0.1:8058',max_size=65536)
            await ws.send(json.dumps({'token':token,'tables':['load_records']}))
            assert json.loads(await asyncio.wait_for(ws.recv(),10))['type']=='ready'
            sockets.append(ws)
        listeners=[asyncio.create_task(listen(ws)) for ws in sockets]
        start=time.perf_counter();deadline=start+seconds
        async def user(index):
            count=0;headers={'Authorization':'Bearer '+tokens[index]}
            await asyncio.sleep(index/clients*.3)
            while time.perf_counter()<deadline:
                t=time.perf_counter()
                try:
                    if count%5==4:
                        r=await api.post('/api/data/load_records',headers=headers,json={'title':'progress'})
                    else:
                        r=await api.get('/api/data/load_records?limit=20&include_total=false',headers=headers)
                    if r.status_code not in (200,201):errors[str(r.status_code)]=errors.get(str(r.status_code),0)+1
                except Exception as exc:
                    key=type(exc).__name__;errors[key]=errors.get(key,0)+1
                http_lat.append(time.perf_counter()-t);count+=1
                if pace:await asyncio.sleep(pace)
        await asyncio.gather(*(user(i) for i in range(clients)))
        elapsed=time.perf_counter()-start
        await asyncio.sleep(2)
        for task in listeners:task.cancel()
        await asyncio.gather(*listeners,return_exceptions=True)
        await asyncio.gather(*(ws.close() for ws in sockets))
    return {'clients':clients,'websockets':clients,'seconds':round(elapsed,2),'think_time_seconds':pace,'requests':len(http_lat),'requests_per_second':round(len(http_lat)/elapsed,2),'http_errors':errors,'p50_ms':percentile(http_lat,.5),'p95_ms':percentile(http_lat,.95),'p99_ms':percentile(http_lat,.99),'websocket_events':events,'websocket_errors':ws_errors}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--reset-test-database',action='store_true',required=True);parser.add_argument('--seconds',type=int,default=20);parser.add_argument('--output',type=Path,default=Path('/tmp/setapi-load-report.json'));args=parser.parse_args()
    dsn=os.environ['DATABASE_URL'];info=conninfo_to_dict(dsn);redis=os.environ['REDIS_URL']
    if info.get('dbname')!='setapi_load_test' or info.get('host') not in ('localhost','127.0.0.1') or urlsplit(redis).hostname not in ('localhost','127.0.0.1') or urlsplit(redis).path!='/13':
        raise SystemExit('Requires local database setapi_load_test and dedicated local Redis database /13')
    with psycopg.connect(dsn) as conn:
        conn.execute('DROP SCHEMA IF EXISTS setapi CASCADE');conn.execute('DROP SCHEMA IF EXISTS data CASCADE')
    os.environ.update(SETAPI_ENCRYPTION_KEY=Fernet.generate_key().decode(),SETAPI_ADMIN_PASSWORD=secrets.token_urlsafe(24),SETAPI_PUBLIC_URL='http://127.0.0.1:8058',SETAPI_COOKIE_SECURE='false')
    from app import db,tables,postgrest
    from app.security import issue,hash_password
    db.start();tokens=[];tenant=uuid4()
    with db.connection() as conn:
        conn.execute('INSERT INTO setapi.organizations(id,name) VALUES(%s,%s)',(tenant,'Load test organization'))
        conn.execute('CREATE TABLE data.load_records(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),owner uuid,tenant uuid,title text)')
        tables.manage(conn,'load_records')
        conn.execute("INSERT INTO setapi.policies VALUES('load_records','owner','tenant',%s,%s)",(Jsonb(['id','title','created_at']),Jsonb(['title'])))
        postgrest.protect(conn, 'load_records')
        conn.execute('CREATE INDEX load_owner_idx ON data.load_records(owner,tenant,created_at,id)')
        encoded=hash_password(secrets.token_urlsafe(24))
        for i in range(200):
            row=conn.execute("INSERT INTO setapi.users(email,password_hash,role,audience,tenant_id,scopes) VALUES(%s,%s,'member','app',%s,%s) RETURNING id",(f'load{i}@test.invalid',encoded,tenant,Jsonb({'load_records':['read','create']}))).fetchone()
            tokens.append(issue(conn,row['id'],'Load test','session',{'load_records':['read','create']})['token'])
            conn.execute("INSERT INTO data.load_records(owner,tenant,title) SELECT %s,%s,'Lesson '||i FROM generate_series(1,50) i",(row['id'],tenant))
        conn.execute('DELETE FROM setapi.outbox')
    # Dedicated Redis DB only; this database is reserved by the guard above.
    db.cache.flushdb();db.stop()
    log=open('/tmp/setapi-load-server.log','w')
    api=subprocess.Popen([sys.executable,'-m','uvicorn','app.main:app','--host','127.0.0.1','--port','8058','--no-access-log','--timeout-keep-alive','15'],cwd=ROOT,stdout=log,stderr=log)
    worker=subprocess.Popen([sys.executable,'-m','app.worker'],cwd=ROOT,stdout=log,stderr=log)
    rest=subprocess.Popen([sys.executable,'-m','app.postgrest'],cwd=ROOT,stdout=log,stderr=log) if postgrest.enabled() else None
    processes=[api,worker]+([rest] if rest else [])
    try:
        for _ in range(100):
            try:
                if httpx.get('http://127.0.0.1:8058/health/ready').status_code==200:break
            except httpx.HTTPError:pass
            time.sleep(.2)
        else:
            raise RuntimeError('Load test stack did not become ready')
        report={'read_engine':os.getenv('SETAPI_READ_ENGINE','native'),'postgrest_version':'16.4' if rest else None,'postgrest_threads':int(os.getenv('SETAPI_POSTGREST_THREADS','1')) if rest else None,'http_keepalive_seconds':15,'date_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'cpu_count':os.cpu_count(),'cpu_affinity':len(os.sched_getaffinity(0)),'database_rows_initial':10000,'api_processes':1,'db_pool_max':20,'notes':'Local shared machine; not a production capacity guarantee. Pre-issued individual student sessions; excludes login/hash cost, uploads and backups. 80% list reads, 20% inserts, owner+tenant+field policy. Includes active WebSockets.','scenarios':[]}
        for clients,pace in [(100,2),(200,2),(100,0)]:
            row=asyncio.run(scenario(tokens,clients,args.seconds,pace));report['scenarios'].append(row);print(json.dumps(row),flush=True)
        report['api_alive']=api.poll() is None;report['worker_alive']=worker.poll() is None
        for name,proc in [('api',api),('worker',worker)]+([('postgrest',rest)] if rest else []):
            status=Path(f'/proc/{proc.pid}/status').read_text()
            report[name+'_peak_rss_kb']=int(next(line for line in status.splitlines() if line.startswith('VmHWM:')).split()[1])
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,indent=2)+'\n');print('Report:',args.output)
    finally:
        for proc in processes:proc.terminate()
        for proc in processes:
            try:proc.wait(timeout=15)
            except subprocess.TimeoutExpired:proc.kill();proc.wait()
        log.close()


if __name__=='__main__':main()
