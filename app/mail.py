import os
import smtplib
import ssl
from email.message import EmailMessage
from . import db, storage


def send_one():
    host=os.getenv('SETAPI_SMTP_HOST')
    sender=os.getenv('SETAPI_SMTP_FROM')
    if not host or not sender:return
    with db.connection() as conn:
        job=conn.execute('SELECT * FROM setapi.mail_queue WHERE attempts<5 AND next_attempt<=now() ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED').fetchone()
        if not job:return
        try:
            payload=storage.decrypt(job['payload_encrypted'])
            message=EmailMessage();message['From']=sender;message['To']=payload['to'];message['Subject']=payload['subject'];message.set_content(payload['text'])
            port=int(os.getenv('SETAPI_SMTP_PORT','587'))
            context=ssl.create_default_context()
            if port==465:
                smtp=smtplib.SMTP_SSL(host,port,timeout=10,context=context)
            else:
                smtp=smtplib.SMTP(host,port,timeout=10);smtp.ehlo();smtp.starttls(context=context);smtp.ehlo()
            with smtp:
                if os.getenv('SETAPI_SMTP_USER'):smtp.login(os.environ['SETAPI_SMTP_USER'],os.environ['SETAPI_SMTP_PASSWORD'])
                smtp.send_message(message)
            conn.execute('DELETE FROM setapi.mail_queue WHERE id=%s',(job['id'],))
        except Exception:
            conn.execute("UPDATE setapi.mail_queue SET attempts=attempts+1,next_attempt=now()+interval '1 minute' WHERE id=%s",(job['id'],))


def cleanup():
    with db.connection() as conn:
        conn.execute('DELETE FROM setapi.action_tokens WHERE expires_at<now()')
        conn.execute("DELETE FROM setapi.mail_queue WHERE attempts>=5 OR next_attempt<now()-interval '1 day'")
