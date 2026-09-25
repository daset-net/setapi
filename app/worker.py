import json
import logging
import signal
import threading
from . import db, storage
from .backup import create_backup

log = logging.getLogger('setapi.worker')
stopping = threading.Event()


def publish_events():
    with db.connection() as conn:
        rows = conn.execute('SELECT * FROM setapi.outbox ORDER BY id LIMIT 100 FOR UPDATE SKIP LOCKED').fetchall()
        for row in rows:
            db.cache.publish('setapi:events', json.dumps(dict(row['event'], event_id=row['id'])))
            conn.execute('DELETE FROM setapi.outbox WHERE id=%s', (row['id'],))
    return len(rows)


def schedule_jobs():
    with db.connection() as conn:
        rows = conn.execute('SELECT * FROM setapi.schedules WHERE enabled AND next_run<=now() FOR UPDATE SKIP LOCKED').fetchall()
        for row in rows:
            if not conn.execute("SELECT 1 FROM setapi.backups WHERE schedule_id=%s AND status IN ('queued','running')", (row['id'],)).fetchone():
                conn.execute('INSERT INTO setapi.backups(storage_id,schedule_id) VALUES(%s,%s)', (row['storage_id'], row['id']))
            conn.execute("UPDATE setapi.schedules SET next_run=now()+every_hours*interval '1 hour' WHERE id=%s", (row['id'],))


def run_backup():
    with db.connection() as conn:
        # One global backup at a time; transaction lock is released on process death.
        if not conn.execute('SELECT pg_try_advisory_xact_lock(73288102) AS acquired').fetchone()['acquired']:
            return False
        # A prior crashed worker left a visible running job. Mark it failed, never silently completed.
        conn.execute("UPDATE setapi.backups SET status='failed',error='Worker interrupted; request a new backup',finished_at=now() WHERE status='running'")
        job = conn.execute("SELECT * FROM setapi.backups WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
        if not job:
            return False
        # Commit progress independently while the global advisory lock prevents
        # another worker from claiming the job. The panel can now see 'running'.
        with db.connection() as progress:
            progress.execute("UPDATE setapi.backups SET status='running' WHERE id=%s", (job['id'],))
        try:
            key, size, checksum = create_backup(job)
            conn.execute("UPDATE setapi.backups SET status='completed',object_key=%s,size=%s,checksum=%s,finished_at=now() WHERE id=%s", (key, size, checksum, job['id']))
        except Exception as exc:
            log.error('Backup %s failed (%s)', job['id'], type(exc).__name__)
            conn.execute("UPDATE setapi.backups SET status='failed',error=%s,finished_at=now() WHERE id=%s", ('Backup failed: check database client, storage credentials and connectivity', job['id']))
    return True


def retention():
    with db.connection() as conn:
        if not conn.execute('SELECT pg_try_advisory_xact_lock(73288103) AS acquired').fetchone()['acquired']:
            return
        for schedule in conn.execute('SELECT * FROM setapi.schedules').fetchall():
            rows = conn.execute("SELECT * FROM setapi.backups WHERE schedule_id=%s AND status='completed' ORDER BY created_at DESC OFFSET %s", (schedule['id'], schedule['retention'])).fetchall()
            for row in rows:
                storage.get(conn, row['storage_id']).delete(row['object_key'])
                conn.execute("UPDATE setapi.backups SET status='expired' WHERE id=%s", (row['id'],))


def backup_loop():
    while not stopping.is_set():
        try:
            schedule_jobs()
            run_backup()
            retention()
        except Exception as exc:
            log.error('Backup cycle failed (%s)', type(exc).__name__)
        stopping.wait(5)


def main():
    logging.basicConfig(level=logging.INFO)
    db.start()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopping.set())
    thread = threading.Thread(target=backup_loop, daemon=True)
    thread.start()
    try:
        while not stopping.is_set():
            try:
                db.cache.set('setapi:worker:heartbeat', '1', ex=15)
                publish_events()
            except Exception as exc:
                log.error('Outbox cycle failed (%s)', type(exc).__name__)
            stopping.wait(.25)
    finally:
        thread.join(timeout=10)
        db.stop()


if __name__ == '__main__':
    main()
