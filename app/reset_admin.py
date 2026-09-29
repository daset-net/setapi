"""Recover panel access: apply SETAPI_ADMIN_EMAIL / SETAPI_ADMIN_PASSWORD to the global administrator.

Run from the server console (python -m app.reset_admin). Only someone with shell access to the
service can use it; the web API never exposes this operation.
"""
import argparse
import hashlib
import re
import sys
import psycopg
from argon2 import PasswordHasher
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from redis import Redis
from .config import settings


def main(argv=None):
    parser = argparse.ArgumentParser(description='Apply SETAPI_ADMIN_EMAIL and SETAPI_ADMIN_PASSWORD to the global administrator.')
    parser.add_argument('--disable-mfa', action='store_true', help='Also turn off two-step verification for this account')
    args = parser.parse_args(argv)
    conf = settings()
    email = conf.bootstrap_email.strip().lower()
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
        sys.exit('SETAPI_ADMIN_EMAIL inválido.')
    if len(conf.bootstrap_password) < 12:
        sys.exit('SETAPI_ADMIN_PASSWORD precisa ter pelo menos 12 caracteres.')
    encoded = PasswordHasher().hash(conf.bootstrap_password)
    with psycopg.connect(conf.database_url, row_factory=dict_row) as conn:
        user = conn.execute('SELECT id,role,audience,tenant_id FROM setapi.users WHERE email=%s FOR UPDATE', (email,)).fetchone()
        if user and (user['role'] != 'admin' or user['audience'] != 'panel' or user['tenant_id'] is not None):
            # Never promote an organization member or app account to global administrator.
            sys.exit(f'{email} já pertence a um usuário que não é administrador global. Use outro e-mail em SETAPI_ADMIN_EMAIL.')
        if user:
            mfa = ',mfa_secret=NULL,mfa_step=-1,recovery_hashes=%s' if args.disable_mfa else ''
            params = (encoded, Jsonb([]), user['id']) if args.disable_mfa else (encoded, user['id'])
            conn.execute('UPDATE setapi.users SET password_hash=%s,active=true' + mfa + ' WHERE id=%s', params)
            action = 'Senha redefinida'
        else:
            user = conn.execute("INSERT INTO setapi.users(email,password_hash,role) VALUES(%s,%s,'admin') RETURNING id", (email, encoded)).fetchone()
            action = 'Administrador criado'
        conn.execute('UPDATE setapi.tokens SET revoked_at=now() WHERE user_id=%s AND revoked_at IS NULL', (user['id'],))
        conn.execute('DELETE FROM setapi.action_tokens WHERE user_id=%s', (user['id'],))
        conn.execute('INSERT INTO setapi.audit(user_id,action,resource,details) VALUES(%s,%s,%s,%s)',
                     (user['id'], 'auth.reset_admin', 'console', Jsonb({'mfa_disabled': args.disable_mfa})))
    try:
        # Failed attempts before the reset should not keep the account locked out.
        Redis.from_url(conf.redis_url, socket_connect_timeout=3).delete('setapi:login:email:' + hashlib.sha256(email.encode()).hexdigest())
    except Exception:
        pass
    print(f'{action}: {email}. Entre no painel com a senha de SETAPI_ADMIN_PASSWORD.')


if __name__ == '__main__':
    main()
