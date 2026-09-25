CREATE SCHEMA IF NOT EXISTS setapi;
CREATE SCHEMA IF NOT EXISTS data;
CREATE TABLE IF NOT EXISTS setapi.users (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), email text NOT NULL UNIQUE,
 password_hash text NOT NULL, role text NOT NULL CHECK(role IN ('admin','member')),
 active boolean NOT NULL DEFAULT true, scopes jsonb NOT NULL DEFAULT '{}',
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS setapi.tokens (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), user_id uuid NOT NULL REFERENCES setapi.users(id),
 name text NOT NULL, digest text NOT NULL UNIQUE, prefix text NOT NULL,
 kind text NOT NULL CHECK(kind IN ('session','api')), scopes jsonb NOT NULL DEFAULT '{}',
 admin boolean NOT NULL DEFAULT false, expires_at timestamptz NOT NULL,
 revoked_at timestamptz, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS setapi.storages (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), name text NOT NULL UNIQUE,
 provider text NOT NULL CHECK(provider IN ('s3','r2','drive')),
 config_encrypted text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS setapi.files (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), storage_id uuid NOT NULL REFERENCES setapi.storages(id),
 owner_id uuid NOT NULL REFERENCES setapi.users(id), name text NOT NULL, object_key text NOT NULL,
 content_type text NOT NULL, size bigint NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS setapi.audit (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, user_id uuid, action text NOT NULL,
 resource text NOT NULL, details jsonb NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS setapi.outbox (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, table_name text NOT NULL,
 event jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS setapi.schedules (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), storage_id uuid NOT NULL REFERENCES setapi.storages(id),
 every_hours integer NOT NULL CHECK(every_hours BETWEEN 1 AND 8760),
 retention integer NOT NULL DEFAULT 7 CHECK(retention BETWEEN 1 AND 365),
 enabled boolean NOT NULL DEFAULT true, next_run timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS setapi.backups (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), storage_id uuid NOT NULL REFERENCES setapi.storages(id),
 schedule_id uuid REFERENCES setapi.schedules(id) ON DELETE SET NULL,
 status text NOT NULL DEFAULT 'queued', object_key text, size bigint, checksum text, error text,
 created_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz
);

CREATE TABLE IF NOT EXISTS setapi.integrations (
 name text PRIMARY KEY, value_encrypted text NOT NULL
);

ALTER TABLE setapi.users ADD COLUMN IF NOT EXISTS audience text NOT NULL DEFAULT 'panel';
ALTER TABLE setapi.users ADD COLUMN IF NOT EXISTS tenant_id uuid;
ALTER TABLE setapi.users ADD COLUMN IF NOT EXISTS mfa_secret text;
ALTER TABLE setapi.users ADD COLUMN IF NOT EXISTS mfa_step bigint NOT NULL DEFAULT -1;
ALTER TABLE setapi.users ADD COLUMN IF NOT EXISTS recovery_hashes jsonb NOT NULL DEFAULT '[]';
CREATE TABLE IF NOT EXISTS setapi.policies (
 table_name text PRIMARY KEY, owner_column text, tenant_column text,
 read_fields jsonb NOT NULL DEFAULT '[]', write_fields jsonb NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS setapi.action_tokens (
 digest text PRIMARY KEY, user_id uuid NOT NULL REFERENCES setapi.users(id),
 purpose text NOT NULL, expires_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS setapi.mail_queue (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, payload_encrypted text NOT NULL,
 attempts integer NOT NULL DEFAULT 0, next_attempt timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS tokens_user_idx ON setapi.tokens(user_id);
CREATE INDEX IF NOT EXISTS files_owner_idx ON setapi.files(owner_id);
CREATE INDEX IF NOT EXISTS backups_status_idx ON setapi.backups(status,created_at);
CREATE INDEX IF NOT EXISTS schedules_due_idx ON setapi.schedules(next_run) WHERE enabled;
CREATE OR REPLACE FUNCTION setapi.capture_change() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE r jsonb; route jsonb := '{}'::jsonb; p record;
BEGIN
 IF TG_OP='DELETE' THEN r=to_jsonb(OLD); ELSE r=to_jsonb(NEW); END IF;
 SELECT * INTO p FROM setapi.policies WHERE table_name=TG_TABLE_NAME;
 IF FOUND THEN
  IF p.owner_column IS NOT NULL THEN route=route || jsonb_build_object(p.owner_column,r->p.owner_column); END IF;
  IF p.tenant_column IS NOT NULL THEN route=route || jsonb_build_object(p.tenant_column,r->p.tenant_column); END IF;
 END IF;
 INSERT INTO setapi.outbox(table_name,event) VALUES(TG_TABLE_NAME,jsonb_build_object('table',TG_TABLE_NAME,'id',r->>'id','operation',CASE TG_OP WHEN 'INSERT' THEN 'created' WHEN 'UPDATE' THEN 'updated' ELSE 'deleted' END,'_row',route));
 RETURN NULL;
END $$;
CREATE TABLE IF NOT EXISTS setapi.organizations (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), name text NOT NULL UNIQUE,
 active boolean NOT NULL DEFAULT true, created_at timestamptz NOT NULL DEFAULT now()
);
-- Preserve existing tenant identifiers when upgrading the preliminary tenant implementation.
INSERT INTO setapi.organizations(id,name)
SELECT DISTINCT tenant_id,'Organização importada '||tenant_id::text FROM setapi.users
WHERE tenant_id IS NOT NULL ON CONFLICT(id) DO NOTHING;
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conname='users_organization_fk' AND conrelid='setapi.users'::regclass) THEN
  ALTER TABLE setapi.users ADD CONSTRAINT users_organization_fk FOREIGN KEY(tenant_id) REFERENCES setapi.organizations(id) ON DELETE RESTRICT;
 END IF;
END $$;
CREATE INDEX IF NOT EXISTS users_organization_idx ON setapi.users(tenant_id);
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM information_schema.columns WHERE table_schema='setapi' AND table_name='files' AND column_name='organization_id') THEN
  ALTER TABLE setapi.files ADD COLUMN organization_id uuid REFERENCES setapi.organizations(id) ON DELETE RESTRICT;
  UPDATE setapi.files f SET organization_id=u.tenant_id FROM setapi.users u WHERE f.owner_id=u.id;
 END IF;
END $$;
