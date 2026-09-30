"""Complete export of an organization's tables as JSON, CSV (one file per table in a ZIP) or XLSX (one sheet per table)."""
import csv
import io
import json
import os
import re
import secrets
import tempfile
import unicodedata
import zipfile
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID
from xml.sax.saxutils import escape
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from psycopg import sql
from starlette.background import BackgroundTask
from . import db, tables
from .security import audit, builder

router = APIRouter(prefix='/api', tags=['Export'])
FORMATS = {'json': ('application/json', 'json'), 'csv': ('application/zip', 'zip'),
           'xlsx': ('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'xlsx')}
XLSX_ROWS = 1_048_575  # Excel's sheet limit, minus the header row
CONTROL = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f]')


def organization_tables(conn, prefix, only):
    rows = conn.execute("SELECT table_name AS name FROM information_schema.tables WHERE table_schema='data' AND table_type='BASE TABLE' AND starts_with(table_name,%s) ORDER BY table_name",
                        (prefix,)).fetchall()
    chosen = [r['name'] for r in rows if tables.PREFIXED.match(r['name'])]
    if only:
        chosen = [p for p in chosen if tables.logical(p) == only]
        if not chosen:
            raise HTTPException(404, 'Table not found')
    return chosen


def records(conn, physical, names):
    # A named cursor streams the table instead of loading it in memory.
    with conn.cursor(name='export_' + secrets.token_hex(8)) as cursor:
        cursor.itersize = 500
        cursor.execute(sql.SQL('SELECT {} FROM data.{} ORDER BY created_at, id').format(
            sql.SQL(',').join(map(sql.Identifier, names)), sql.Identifier(physical)))
        yield from cursor


def text(value):
    if value is None:
        return ''
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return value.isoformat() if hasattr(value, 'isoformat') else str(value)


def xlsx_cell(value):
    if isinstance(value, bool):
        return f'<c t="b"><v>{int(value)}</v></c>'
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return f'<c><v>{value}</v></c>'
    content = CONTROL.sub('', text(value))[:32767]
    return f'<c t="inlineStr"><is><t xml:space="preserve">{escape(content)}</t></is></c>'


def sheet_title(name, taken):
    title = re.sub(r'[\[\]:*?/\\]', '_', name)[:31] or 'tabela'
    base, n = title, 2
    while title.lower() in taken:
        title = f'{base[:28]}_{n}'
        n += 1
    taken.add(title.lower())
    return title


def write_xlsx(conn, path, chosen):
    taken, sheets = set(), []
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
        for index, physical in enumerate(chosen, 1):
            names = [c['name'] for c in tables.columns(conn, physical)]
            sheets.append(sheet_title(tables.logical(physical), taken))
            with archive.open(f'xl/worksheets/sheet{index}.xml', 'w') as out:
                out.write(b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>')
                out.write(('<row>' + ''.join(xlsx_cell(n) for n in names) + '</row>').encode())
                for count, row in enumerate(records(conn, physical, names), 1):
                    if count > XLSX_ROWS:
                        raise HTTPException(413, f'A tabela {tables.logical(physical)} passa do limite de linhas do Excel. Exporte em CSV.')
                    out.write(('<row>' + ''.join(xlsx_cell(row[n]) for n in names) + '</row>').encode())
                out.write(b'</sheetData></worksheet>')
        if not sheets:
            sheets.append('vazio')
            archive.writestr('xl/worksheets/sheet1.xml', '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData/></worksheet>')
        main = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
        rel = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
        archive.writestr('[Content_Types].xml', '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                         + ''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1, len(sheets) + 1)) + '</Types>')
        archive.writestr('_rels/.rels', f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="{rel}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr('xl/workbook.xml', f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="{main}" xmlns:r="{rel}"><sheets>'
                         + ''.join(f'<sheet name="{escape(t)}" sheetId="{i}" r:id="rId{i}"/>' for i, t in enumerate(sheets, 1)) + '</sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels', '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                         + ''.join(f'<Relationship Id="rId{i}" Type="{rel}/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1, len(sheets) + 1))
                         + f'<Relationship Id="rId{len(sheets) + 1}" Type="{rel}/styles" Target="styles.xml"/></Relationships>')
        archive.writestr('xl/styles.xml', f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="{main}"><fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts><fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs><cellXfs count="1"><xf/></cellXfs></styleSheet>')


def write_csv(conn, path, chosen):
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
        for physical in chosen:
            names = [c['name'] for c in tables.columns(conn, physical)]
            with archive.open(tables.logical(physical) + '.csv', 'w') as raw:
                # UTF-8 with BOM so Excel opens accents correctly.
                out = io.TextIOWrapper(raw, encoding='utf-8-sig', newline='')
                writer = csv.writer(out)
                writer.writerow(names)
                for row in records(conn, physical, names):
                    writer.writerow([text(row[n]) for n in names])
                out.flush()
                out.detach()


def write_json(conn, path, chosen, organization):
    with open(path, 'w', encoding='utf-8') as out:
        out.write(json.dumps({'organization': organization['name'], 'exported_at': datetime.now(timezone.utc).isoformat()}, ensure_ascii=False)[:-1] + ',"tables":{')
        for i, physical in enumerate(chosen):
            names = [c['name'] for c in tables.columns(conn, physical)]
            out.write((',' if i else '') + json.dumps(tables.logical(physical)) + ':[')
            for j, row in enumerate(records(conn, physical, names)):
                out.write((',' if j else '') + json.dumps({n: row[n] for n in names}, ensure_ascii=False, default=text))
            out.write(']')
        out.write('}}')


@router.get('/export')
def export(format: str = Query('xlsx', description='json, csv (a ZIP with one file per table) or xlsx (one sheet per table).'),
           table: str = Query('', description='Only this table; every table of the organization when empty.'),
           organization_id: UUID | None = Query(None, description='Organization to export; administrators only. Organization administrators always export their own.'),
           user=Depends(builder)):
    """Download every record of the organization's own tables."""
    if format not in FORMATS:
        raise HTTPException(422, 'Use json, csv or xlsx')
    fd, path = tempfile.mkstemp(prefix='setapi-export-')
    os.close(fd)
    try:
        with db.connection() as conn:
            organization_id = tables.context(conn, user, organization_id)
            if organization_id is None:
                raise HTTPException(422, 'Escolha uma organização para exportar os dados dela.')
            organization = conn.execute('SELECT name,table_prefix FROM setapi.organizations WHERE id=%s', (organization_id,)).fetchone()
            chosen = organization_tables(conn, organization['table_prefix'], table)
            if format == 'xlsx':
                write_xlsx(conn, path, chosen)
            elif format == 'csv':
                write_csv(conn, path, chosen)
            else:
                write_json(conn, path, chosen, organization)
            audit(conn, user, 'data.export', str(organization_id), {'format': format, 'tables': [tables.logical(p) for p in chosen]})
    except BaseException:
        os.unlink(path)
        raise
    media, extension = FORMATS[format]
    plain = unicodedata.normalize('NFKD', organization['name']).encode('ascii', 'ignore').decode().lower()
    slug = re.sub(r'[^a-z0-9]+', '-', plain).strip('-') or 'organizacao'
    name = f'{slug}{"-" + table if table else ""}-{datetime.now().strftime("%Y-%m-%d")}.{extension}'
    return FileResponse(path, filename=name, media_type=media, background=BackgroundTask(os.unlink, path),
                        headers={'Cache-Control': 'no-store'})
