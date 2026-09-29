import os
import smtplib
import ssl
from email.message import EmailMessage
from . import db, storage, mail_providers

ADMINS="SELECT email FROM setapi.users WHERE role='admin' AND tenant_id IS NULL AND audience='panel' AND active"
# Panel users: global administrators and each organization's panel users. App users (students) are never reached.
PANEL="""SELECT u.email FROM setapi.users u LEFT JOIN setapi.organizations o ON o.id=u.tenant_id
 WHERE u.audience='panel' AND u.active AND (u.tenant_id IS NULL OR o.active)"""


def provider_config():
    with db.connection() as conn:
        row=conn.execute("SELECT value_encrypted FROM setapi.integrations WHERE name='mail'").fetchone()
    config=storage.decrypt(row['value_encrypted']) if row else None
    # A saved provider that is no longer offered, or an older shape, counts as not configured.
    if not config or config.get('provider') not in mail_providers.PROVIDERS or not config.get('inbox_id'):
        return None
    return config


def smtp_ready():
    return bool(os.getenv('SETAPI_SMTP_HOST') and os.getenv('SETAPI_SMTP_FROM'))


def ready():
    return bool(provider_config() or smtp_ready())


def is_panel_address(conn,email):
    return conn.execute(PANEL+' AND u.email=%s',(email.lower(),)).fetchone() is not None


def can_reach(conn,user):
    """The panel provider only writes to panel users; legacy SMTP reaches anyone."""
    if smtp_ready():return True
    return bool(provider_config()) and is_panel_address(conn,user['email'])


def queue(conn,to,subject,text):
    conn.execute('INSERT INTO setapi.mail_queue(payload_encrypted) VALUES(%s)',(storage.encrypt({'to':to,'subject':subject,'text':text}),))


def notify_admins(conn,subject,text):
    """Queue a notification for every active global administrator."""
    if not ready():return
    for row in conn.execute(ADMINS).fetchall():
        queue(conn,row['email'],'SETAPI — '+subject,text)


def notify_organization(conn,organization_id,subject,text):
    """Queue a notification for the panel users of one organization."""
    if not ready():return
    for row in conn.execute(PANEL+' AND u.tenant_id=%s',(organization_id,)).fetchall():
        queue(conn,row['email'],'SETAPI — '+subject,text)


def send_smtp(payload):
    host=os.environ['SETAPI_SMTP_HOST']
    message=EmailMessage();message['From']=os.environ['SETAPI_SMTP_FROM'];message['To']=payload['to'];message['Subject']=payload['subject'];message.set_content(payload['text'])
    port=int(os.getenv('SETAPI_SMTP_PORT','587'))
    context=ssl.create_default_context()
    if port==465:
        smtp=smtplib.SMTP_SSL(host,port,timeout=10,context=context)
    else:
        smtp=smtplib.SMTP(host,port,timeout=10);smtp.ehlo();smtp.starttls(context=context);smtp.ehlo()
    with smtp:
        if os.getenv('SETAPI_SMTP_USER'):smtp.login(os.environ['SETAPI_SMTP_USER'],os.environ['SETAPI_SMTP_PASSWORD'])
        smtp.send_message(message)


def send_one():
    api=provider_config()
    if not api and not smtp_ready():return
    with db.connection() as conn:
        job=conn.execute('SELECT * FROM setapi.mail_queue WHERE attempts<5 AND next_attempt<=now() ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED').fetchone()
        if not job:return
        try:
            payload=storage.decrypt(job['payload_encrypted'])
            if api and is_panel_address(conn,payload['to']):
                # The panel provider takes precedence, and only ever writes to panel users.
                mail_providers.send(api,payload['to'],payload['subject'],payload['text'],'setapi-mail-'+str(job['id']))
            elif smtp_ready():
                send_smtp(payload)
            # Anything else has no allowed route and is dropped.
            conn.execute('DELETE FROM setapi.mail_queue WHERE id=%s',(job['id'],))
        except Exception:
            conn.execute("UPDATE setapi.mail_queue SET attempts=attempts+1,next_attempt=now()+interval '1 minute' WHERE id=%s",(job['id'],))


def cleanup():
    with db.connection() as conn:
        conn.execute('DELETE FROM setapi.action_tokens WHERE expires_at<now()')
        conn.execute("DELETE FROM setapi.mail_queue WHERE attempts>=5 OR next_attempt<now()-interval '1 day'")
