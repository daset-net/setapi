"""Platform-wide settings the global administrator changes in the panel: branding and limits."""
import re
import time
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from psycopg.types.json import Jsonb
from . import db
from .config import settings
from .security import admin, audit

router = APIRouter(prefix='/api/platform', tags=['Platform settings'])
TOKEN_HOURS = (168, 720, 1440, 2160, 4320, 8760)


def defaults():
    return {'name': 'SETAPI', 'login_message': 'Entre para gerenciar seu ambiente.', 'color': '#087a5a',
            'session_hours': 12, 'max_upload_mb': settings().max_upload_mb, 'token_hours': 720}


_cache = {'at': 0.0, 'value': None}


def current():
    # Read on every login and upload; a short cache spares the database, and each change clears it.
    if _cache['value'] is None or time.monotonic() - _cache['at'] > 30:
        with db.connection() as conn:
            row = conn.execute("SELECT value FROM setapi.platform_settings WHERE name='platform'").fetchone()
        _cache.update(value={**defaults(), **(row['value'] if row else {})}, at=time.monotonic())
    return _cache['value']


def forget():
    _cache['value'] = None


class PlatformSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=40)
    login_message: str = Field(default='', max_length=160)
    color: str = Field(description='Main color of the panel, as #rrggbb.')
    session_hours: int = Field(ge=1, le=720, description='How long a panel or app login lasts.')
    max_upload_mb: int = Field(ge=1, le=2048, description='Largest file accepted by the storage upload.')
    token_hours: int = Field(description='Validity suggested when a new token is created.')

    @field_validator('color')
    @classmethod
    def hex_color(cls, value):
        if not re.fullmatch(r'#[0-9a-fA-F]{6}', value):
            raise ValueError('Use uma cor no formato #rrggbb.')
        return value.lower()

    @field_validator('token_hours')
    @classmethod
    def token_choice(cls, value):
        if value not in TOKEN_HOURS:
            raise ValueError('Validade de token não disponível.')
        return value


@router.get('/branding')
def branding():
    """Public: the login screen shows the platform's name before anyone signs in."""
    value = current()
    return {k: value[k] for k in ('name', 'login_message', 'color', 'token_hours')}


@router.get('/settings')
def get_settings(user=Depends(admin)):
    return current()


@router.put('/settings')
def put_settings(body: PlatformSettings, user=Depends(admin)):
    value = body.model_dump()
    value['name'] = value['name'].strip()
    with db.connection() as conn:
        conn.execute("INSERT INTO setapi.platform_settings(name,value) VALUES('platform',%s) ON CONFLICT(name) DO UPDATE SET value=EXCLUDED.value",
                     (Jsonb(value),))
        audit(conn, user, 'platform.settings', 'platform', value)
    forget()
    return current()
