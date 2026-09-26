import base64
import importlib.util
import json
import os
import signal
import stat
import subprocess
import sys
from pathlib import Path
import pytest
import yaml
import app

ROOT = Path(app.__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import env_config
import install
from app.launcher import supervise, commands


def test_generated_env_matches_three_services(tmp_path):
    values = env_config.new_values('admin@example.com', 'https://api.example.com')
    assert '@setapi_db:5432/setapi' in values['DATABASE_URL']
    assert '@setapi_redis:6379/0' in values['REDIS_URL']
    assert values['POSTGRES_PASSWORD'] != values['REDIS_PASSWORD']
    assert len(base64.urlsafe_b64decode(values['SETAPI_ENCRYPTION_KEY'])) == 32
    assert values['SETAPI_COOKIE_SECURE'] == 'true'
    path = tmp_path / '.env'
    env_config.write_private(path, env_config.encode_env(values))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert env_config.load_env(path) == values
    with pytest.raises(FileExistsError):
        env_config.write_private(path, 'OVERWRITE')
    assert env_config.load_env(path) == values
    compose = yaml.safe_load((ROOT / 'compose.yaml').read_text())
    assert set(compose['services']) == set(env_config.SERVICES)
    for name, service in compose['services'].items():
        assert service['container_name'] == name
        if name != 'setapi_app':
            assert 'ports' not in service


def test_easypanel_secrets_and_dns_agree(tmp_path, monkeypatch):
    monkeypatch.setattr(install, 'ROOT', tmp_path)
    install.main(['--target', 'easypanel'])
    target = tmp_path / 'conexao' / 'instalacao'
    schema = json.loads((target / 'easypanel-template.json').read_text())
    services = {s['data']['serviceName']: s['data'] for s in schema['services']}
    assert set(services) == set(env_config.SERVICES)
    pg, redis, service = services['setapi_db'], services['setapi_redis'], services['setapi_app']
    assert pg['password'] in service['env'] and redis['password'] in service['env']
    assert '$(PROJECT_NAME)_setapi_db' in service['env']
    assert '$(PROJECT_NAME)_setapi_redis' in service['env']
    assert 'SETAPI_PUBLIC_URL=https://$(EASYPANEL_DOMAIN)' in service['env']
    assert 'SETAPI_COOKIE_SECURE=true' in service['env']
    assert service['domains'][0]['port'] == 8055
    assert service['build']['type'] == 'dockerfile'
    assert len(service['source']) == 4
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in target.iterdir())
    before = (target / 'easypanel-template.json').read_bytes()
    with pytest.raises(ValueError):
        install.main(['--target', 'easypanel'])
    assert (target / 'easypanel-template.json').read_bytes() == before


def test_docker_installer_reuses_env_without_printing_secrets(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(install, 'ROOT', tmp_path)
    install.main(['--generate-only'])
    initial = (tmp_path / '.env').read_bytes()
    values = env_config.load_env(tmp_path / '.env')
    install.main(['--generate-only'])
    assert initial == (tmp_path / '.env').read_bytes()
    output = capsys.readouterr().out
    for key in ['POSTGRES_PASSWORD','REDIS_PASSWORD','SETAPI_ADMIN_PASSWORD','SETAPI_ENCRYPTION_KEY']:
        assert values[key] not in output
    with pytest.raises(ValueError):
        install.main(['--generate-only', '--admin-email', 'other@example.com'])


def test_installer_validates_before_writing(tmp_path, monkeypatch):
    monkeypatch.setattr(install, 'ROOT', tmp_path)
    with pytest.raises(ValueError):
        install.main(['--target', 'easypanel', '--public-url', 'http://insecure.example.com'])
    assert not (tmp_path / 'conexao').exists()
    with pytest.raises(ValueError):
        env_config.new_values('admin@example.com\nINJECT=1')
    with pytest.raises(ValueError):
        env_config.new_values(public_url='https://example.com/login')


def test_launcher_stops_sibling_when_worker_exits(tmp_path):
    pidfile = tmp_path / 'sibling.pid'
    slow = [sys.executable, '-c', 'import os,time; from pathlib import Path; Path('+repr(str(pidfile))+').write_text(str(os.getpid())); time.sleep(60)']
    failed = [sys.executable, '-c', 'import time; time.sleep(.4); raise SystemExit(1)']
    assert supervise([slow, failed], grace=2) == 1
    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_launcher_defaults_to_api_and_worker(monkeypatch):
    monkeypatch.setenv('SETAPI_READ_ENGINE', 'native')
    monkeypatch.delenv('SETAPI_RUN_WORKER', raising=False)
    assert len(commands()) == 2
    monkeypatch.setenv('SETAPI_RUN_WORKER', 'false')
    assert len(commands()) == 1
