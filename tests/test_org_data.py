import csv
import io
import json
import shutil
import zipfile
from xml.etree import ElementTree
from app import db, storage, worker
from tests.test_organization_tables import org, org_admin, create

S3 = {'bucket': 'test', 'access_key_id': 'test', 'secret_access_key': 'test'}


def seed(c, organization, headers):
    create(c, 'pacientes', organization, [{'name': 'nome', 'type': 'text'}, {'name': 'idade', 'type': 'integer'}, {'name': 'ativo', 'type': 'boolean'}])
    for name, age in (('José da Silva', 40), ('Ana <Maria> & Cia', 31)):
        assert c.post('/api/data/pacientes', headers=headers, json={'nome': name, 'idade': age, 'ativo': True}).status_code == 201


def test_organization_administrator_exports_json_csv_and_xlsx(admin_client):
    c = admin_client
    a, b = org(c), org(c)
    headers, _, _ = org_admin(c, a)
    seed(c, a, headers)
    create(c, 'segredo', b)
    data = json.loads(c.get('/api/export', headers=headers, params={'format': 'json'}).content)
    assert list(data['tables']) == ['pacientes'] and {r['nome'] for r in data['tables']['pacientes']} == {'José da Silva', 'Ana <Maria> & Cia'}
    response = c.get('/api/export', headers=headers, params={'format': 'csv'})
    assert response.headers['content-disposition'].endswith('.zip"')
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.namelist() == ['pacientes.csv']
        rows = list(csv.DictReader(io.StringIO(archive.read('pacientes.csv').decode('utf-8-sig'))))
    assert {r['nome'] for r in rows} == {'José da Silva', 'Ana <Maria> & Cia'} and rows[0]['ativo'] == 'true'
    with zipfile.ZipFile(io.BytesIO(c.get('/api/export', headers=headers, params={'format': 'xlsx', 'table': 'pacientes'}).content)) as archive:
        sheet = ElementTree.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        workbook = ElementTree.fromstring(archive.read('xl/workbook.xml'))
    ns = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    assert [s.get('name') for s in workbook.find('m:sheets', ns)] == ['pacientes']
    assert len(sheet.findall('.//m:row', ns)) == 3
    assert 'Ana <Maria> & Cia' in [t.text for t in sheet.iter('{%s}t' % ns['m'])]
    # Never another organization's tables, and the platform needs an organization.
    assert c.get('/api/export', headers=headers, params={'format': 'json', 'organization_id': b}).status_code == 404
    assert c.get('/api/export', headers=headers, params={'table': 'segredo'}).status_code == 404
    assert c.get('/api/export', params={'format': 'json'}).status_code == 422
    assert list(json.loads(c.get('/api/export', params={'format': 'json', 'organization_id': b}).content)['tables']) == ['segredo']


def test_organization_administrator_restores_a_previous_backup(admin_client, tmp_path, monkeypatch):
    c = admin_client
    a, b = org(c), org(c)
    headers, _, _ = org_admin(c, a)
    seed(c, a, headers)
    create(c, 'intocada', b)
    vault = tmp_path / 'vault'
    vault.mkdir()
    monkeypatch.setattr(storage.Storage, 'upload', lambda self, path, key, content_type=None: (shutil.copyfile(path, vault / key.replace('/', '_')), key)[1])
    monkeypatch.setattr(storage.Storage, 'download', lambda self, key, path: shutil.copyfile(vault / key.replace('/', '_'), path))
    store = c.post('/api/storages', json={'name': 'drive-org', 'provider': 's3', 'config': S3, 'organization_id': a}).json()
    # The organization administrator sees and makes only its own backups.
    assert c.post('/api/backups', headers=headers, json={'storage_id': store['id']}).status_code == 202
    assert c.get('/api/backups', headers=headers, params={'all': 'true'}).json()['data'][0]['organization_id'] == a
    with db.connection() as conn:
        conn.execute("UPDATE setapi.backups SET status='failed' WHERE status='queued' AND organization_id IS DISTINCT FROM %s", (a,))
    assert worker.run_backup() is True
    point = c.get('/api/backups', headers=headers).json()['data'][0]
    assert point['status'] == 'completed'
    # The mistake: records deleted, a field policy changed and a new table created.
    for row in c.get('/api/data/pacientes', headers=headers).json()['data']:
        c.delete('/api/data/pacientes/' + row['id'], headers=headers)
    create(c, 'errada', a)
    assert c.post('/api/backups/' + point['id'] + '/restore', headers=headers).status_code == 202
    assert c.post('/api/backups/' + point['id'] + '/restore', headers=headers).status_code == 409
    assert worker.run_restore() is True
    done = c.get('/api/restores', headers=headers).json()['data'][0]
    assert done['status'] == 'completed', done['error']
    names = {r['nome'] for r in c.get('/api/data/pacientes', headers=headers).json()['data']}
    assert names == {'José da Silva', 'Ana <Maria> & Cia'}
    assert c.get('/api/data/errada', headers=headers).status_code == 404
    # Change capture still works on the restored table, and the other organization is untouched.
    assert c.post('/api/data/pacientes', headers=headers, json={'nome': 'Depois', 'idade': 1, 'ativo': False}).status_code == 201
    assert c.get('/api/data/intocada', params={'organization_id': b}).status_code == 200
    safety = [x for x in c.get('/api/backups', headers=headers).json()['data'] if x['id'] == done['safety_backup_id']]
    assert safety and safety[0]['label'] == 'Antes da restauração' and safety[0]['status'] == 'completed'
    # Restore points of another organization are out of reach.
    other = org_admin(c, b)[0]
    assert c.post('/api/backups/' + point['id'] + '/restore', headers=other).status_code == 404


def test_organization_administrator_manages_storage_and_folders_by_api(admin_client, monkeypatch):
    c = admin_client
    a, b = org(c), org(c)
    headers, _, _ = org_admin(c, a)
    other, _, _ = org_admin(c, b)
    objects = {}
    monkeypatch.setattr(storage.Storage, 'upload', lambda self, path, key, content_type=None: (objects.__setitem__(key, open(path, 'rb').read()), key)[1])
    monkeypatch.setattr(storage.Storage, 'delete', lambda self, key: objects.pop(key))
    # Connecting storage with the organization administrator token.
    store = c.post('/api/storages', headers=headers, json={'name': 'arquivos', 'provider': 's3', 'config': S3})
    assert store.status_code == 201, store.text
    sid = store.json()['id']
    assert store.json()['organization_id'] == a
    folder = lambda **body: c.post('/api/folders', headers=headers, json={'storage_id': sid, **body})
    docs = folder(name='Documentos').json()
    fotos = folder(name='Fotos').json()
    exames = folder(name='Exames', parent_id=docs['id']).json()
    assert folder(name='documentos').status_code == 409
    assert folder(name='a/b').status_code == 422
    up = c.post('/api/files/' + sid, headers=headers, params={'folder_id': exames['id']}, files={'file': ('laudo.pdf', b'%PDF-1', 'application/pdf')})
    assert up.status_code == 201, up.text
    fid = up.json()['id']
    listed = lambda **q: [f['name'] for f in c.get('/api/files', headers=headers, params={'storage_id': sid, **q}).json()['data']]
    assert listed(folder_id=exames['id']) == ['laudo.pdf'] and listed(root=True) == []
    # Rename and move the file; rename and move folders.
    assert c.patch('/api/files/' + fid, headers=headers, json={'name': 'laudo-2026.pdf', 'folder_id': fotos['id']}).json()['folder_id'] == fotos['id']
    assert listed(folder_id=fotos['id']) == ['laudo-2026.pdf']
    assert c.patch('/api/files/' + fid, headers=headers, json={'folder_id': None}).json()['folder_id'] is None
    assert listed(root=True) == ['laudo-2026.pdf']
    assert c.patch('/api/folders/' + docs['id'], headers=headers, json={'name': 'Documentos 2026'}).json()['name'] == 'Documentos 2026'
    assert c.patch('/api/folders/' + docs['id'], headers=headers, json={'parent_id': exames['id']}).status_code == 422
    assert c.patch('/api/folders/' + exames['id'], headers=headers, json={'parent_id': None}).json()['parent_id'] is None
    names = sorted(f['name'] for f in c.get('/api/folders', headers=headers, params={'storage_id': sid}).json()['data'])
    assert names == ['Documentos 2026', 'Exames', 'Fotos']
    # Another organization sees none of it.
    assert c.get('/api/folders', headers=other, params={'storage_id': sid}).status_code == 404
    assert c.patch('/api/files/' + fid, headers=other, json={'name': 'x'}).status_code == 404
    assert c.delete('/api/folders/' + fotos['id'], headers=other).status_code == 404
    # A folder with content needs recursive=true, and then its files leave the provider too.
    c.patch('/api/files/' + fid, headers=headers, json={'folder_id': fotos['id']})
    folder(name='Viagem', parent_id=fotos['id'])
    assert c.delete('/api/folders/' + fotos['id'], headers=headers).status_code == 409
    assert c.delete('/api/folders/' + fotos['id'], headers=headers, params={'recursive': 'true'}).status_code == 204
    assert objects == {} and listed() == []
    assert c.delete('/api/folders/' + exames['id'], headers=headers).status_code == 204
    # A member token still cannot manage storage.
    member = c.post('/api/users', json={'email': 'm-' + fid[:6] + '@org.test', 'password': 'Member-password-123', 'tenant_id': a}).json()
    token = c.post('/api/tokens', json={'name': 'm', 'user_id': member['id']}).json()['token']
    assert c.post('/api/folders', headers={'Authorization': 'Bearer ' + token}, json={'storage_id': sid, 'name': 'x'}).status_code == 403
