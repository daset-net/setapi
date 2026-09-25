#!/usr/bin/env python3
"""Generate credentials and install exactly three SETAPI services."""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit
from env_config import new_values, encode_env, write_private, load_env, SERVICES

ROOT = Path(__file__).resolve().parent.parent


def easypanel_schema(values, public_url=None):
    env = {key: value for key, value in values.items()
           if key not in ('POSTGRES_PASSWORD', 'REDIS_PASSWORD', 'SETAPI_BIND_HOST', 'SETAPI_PORT')}
    env['DATABASE_URL'] = f"postgresql://setapi:{values['POSTGRES_PASSWORD']}@$(PROJECT_NAME)_setapi_db:5432/setapi"
    env['REDIS_URL'] = f"redis://default:{values['REDIS_PASSWORD']}@$(PROJECT_NAME)_setapi_redis:6379/0"
    env['SETAPI_PUBLIC_URL'] = public_url or 'https://$(EASYPANEL_DOMAIN)'
    env['SETAPI_COOKIE_SECURE'] = 'true'
    env['SETAPI_RUN_WORKER'] = 'true'
    host = urlsplit(public_url).hostname if public_url else '$(EASYPANEL_DOMAIN)'
    if public_url and (not public_url.startswith('https://') or urlsplit(public_url).port not in (None, 443)):
        raise ValueError('No EasyPanel, use uma origem HTTPS com a porta padrão 443')
    return {'services': [
        {'type': 'postgres', 'data': {'serviceName': 'setapi_db', 'databaseName': 'setapi', 'user': 'setapi',
            'image': 'postgres:17-bookworm', 'password': values['POSTGRES_PASSWORD']}},
        {'type': 'redis', 'data': {'serviceName': 'setapi_redis', 'image': 'redis:8-alpine',
            'password': values['REDIS_PASSWORD']}},
        {'type': 'app', 'data': {'serviceName': 'setapi_app',
            'source': {'type': 'git', 'repo': 'https://github.com/daset-net/setapi.git', 'ref': 'main', 'path': '/'},
            'build': {'type': 'dockerfile', 'file': 'Dockerfile'}, 'env': encode_env(env),
            'domains': [{'host': host, 'https': True, 'port': 8055, 'path': '/'}]}}
    ]}


def run(command, capture=False):
    return subprocess.run(command, cwd=ROOT, check=True, text=True,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Instalar SETAPI + PostgreSQL + Redis ou gerar template Custom do EasyPanel')
    parser.add_argument('--target', choices=['docker', 'easypanel'], default='docker')
    parser.add_argument('--generate-only', action='store_true', help='Gerar configuração sem iniciar contêineres')
    parser.add_argument('--admin-email', default=None)
    parser.add_argument('--public-url', default=None)
    parser.add_argument('--bind-host', choices=['127.0.0.1', '0.0.0.0'], default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8055)
    parser.add_argument('--output', type=Path, help='Diretório privado de saída do template EasyPanel')
    args = parser.parse_args(argv)
    if args.output and args.target != 'easypanel':
        parser.error('--output é exclusivo do target easypanel')
    if args.target == 'easypanel':
        destination = args.output or ROOT / 'conexao' / 'instalacao'
        if destination.exists() and any(destination.iterdir()):
            raise ValueError('A pasta de instalação já contém arquivos; reutilize-os ou escolha --output em uma pasta vazia. Os segredos não foram alterados.')
        values = new_values(args.admin_email or 'admin@setapi.local', args.public_url or 'https://setapi.example.com')
        schema = easypanel_schema(values, args.public_url)
        destination.mkdir(parents=True, mode=0o700, exist_ok=True)
        env = schema['services'][2]['data']['env']
        write_private(destination / 'setapi_app.env', env)
        write_private(destination / 'setapi_db.env', encode_env({'POSTGRES_DB': 'setapi', 'POSTGRES_USER': 'setapi', 'POSTGRES_PASSWORD': values['POSTGRES_PASSWORD']}))
        write_private(destination / 'setapi_redis.env', encode_env({'REDIS_PASSWORD': values['REDIS_PASSWORD']}))
        write_private(destination / 'easypanel-template.json', json.dumps(schema, ensure_ascii=False, indent=2) + '\n')
        print(f'Template pronto: {destination / "easypanel-template.json"}')
        print('No EasyPanel: abra um projeto → + Serviço → Templates → Custom; cole o JSON e crie os serviços.')
        print('Serviços: setapi_app, setapi_db e setapi_redis. O template configura as conexões e as ENVs automaticamente.')
        print(f'Login inicial: {values["SETAPI_ADMIN_EMAIL"]}. A senha está em {destination / "setapi_app.env"}.')
        return

    if not args.generate_only:
        if not shutil.which('docker'):
            raise ValueError('Instale Docker Engine + Compose v2. Para só gerar as ENVs, use --generate-only.')
        run(['docker', 'compose', 'version'], capture=True)
        run(['docker', 'info'], capture=True)
    env_path = ROOT / '.env'
    if env_path.exists():
        values = load_env(env_path)
        if (args.admin_email and args.admin_email != values['SETAPI_ADMIN_EMAIL']) or (args.public_url and args.public_url.rstrip('/') != values['SETAPI_PUBLIC_URL']):
            raise ValueError('As opções diferem do .env existente. Revise o arquivo manualmente; o instalador não troca credenciais existentes.')
        print('Reutilizando .env existente; senhas e chave de criptografia preservadas.')
    else:
        values = new_values(args.admin_email or 'admin@setapi.local', args.public_url or f'http://localhost:{args.port}', args.bind_host, args.port)
        write_private(env_path, encode_env(values))
        print(f'ENVs e segredos gerados em {env_path}')
    if args.generate_only:
        print('Configuração pronta. Execute bash install.sh para iniciar os três serviços.')
        return
    compose = ['docker', 'compose', '--env-file', str(env_path), '-f', str(ROOT / 'compose.yaml')]
    run(compose + ['config', '--quiet'])
    previous = run(['docker', 'ps', '-a', '--filter', 'label=com.docker.compose.project=setapi',
                    '--format', '{{.Label "com.docker.compose.service"}}'], capture=True).stdout.splitlines()
    if set(previous) - set(SERVICES):
        raise ValueError('Há serviços de uma instalação antiga do projeto setapi. Migre/pare a stack antiga antes de iniciar esta; nenhum volume foi removido.')
    run(compose + ['up', '-d', '--build', '--wait', '--wait-timeout', '240'])
    print(f'SETAPI instalado: {values["SETAPI_PUBLIC_URL"]}')
    print('Contêineres: setapi_app, setapi_db, setapi_redis. API e worker rodam em setapi_app.')
    print(f'Login: {values["SETAPI_ADMIN_EMAIL"]}; consulte SETAPI_ADMIN_PASSWORD no .env.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        if isinstance(exc, subprocess.CalledProcessError):
            print('Falha em um comando Docker. Confira os logs e os requisitos; seus volumes e segredos foram preservados.', file=sys.stderr)
        else:
            print(str(exc), file=sys.stderr)
        sys.exit(1)
