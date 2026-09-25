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
