CREATE OR REPLACE FUNCTION setapi.postgrest_check() RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog,setapi AS $$
DECLARE
    c jsonb := nullif(current_setting('request.jwt.claims',true),'')::jsonb;
    actor record;
    p jsonb;
    root_access boolean;
BEGIN
    IF current_setting('request.method',true) IS DISTINCT FROM 'GET'
       OR current_setting('request.path',true) IS DISTINCT FROM '/' || (c->>'table') THEN
        RAISE insufficient_privilege;
    END IF;
    SELECT t.admin,t.scopes,u.role,u.scopes AS user_scopes,u.tenant_id,u.audience
      INTO actor FROM setapi.tokens t JOIN setapi.users u ON u.id=t.user_id
      LEFT JOIN setapi.organizations o ON o.id=u.tenant_id
      WHERE t.id=(c->>'token_id')::uuid AND u.id=(c->>'sub')::uuid
      AND t.revoked_at IS NULL AND t.expires_at>now() AND u.active
      AND (u.tenant_id IS NULL OR o.active);
    IF NOT FOUND OR actor.tenant_id::text IS DISTINCT FROM c->>'tenant' THEN
        RAISE insufficient_privilege;
    END IF;
    root_access := actor.role='admin' AND actor.admin AND actor.audience='panel' AND actor.tenant_id IS NULL;
    IF root_access IS DISTINCT FROM (c->>'root')::boolean THEN
        RAISE insufficient_privilege;
    END IF;
    IF NOT root_access THEN
        IF NOT coalesce((actor.scopes->(c->>'table')) ? 'read',false)
           OR (actor.role<>'admin' AND NOT coalesce((actor.user_scopes->(c->>'table')) ? 'read',false)) THEN
            RAISE insufficient_privilege;
        END IF;
        SELECT to_jsonb(policy) INTO p FROM setapi.policies policy WHERE table_name=c->>'table';
        IF (p IS NULL AND actor.role<>'admin')
           OR (actor.tenant_id IS NOT NULL AND p->>'tenant_column' IS NULL)
           OR coalesce(p,'null'::jsonb) IS DISTINCT FROM c->'policy' THEN
            RAISE insufficient_privilege;
        END IF;
    END IF;
END;
$$;
REVOKE ALL ON FUNCTION setapi.postgrest_check() FROM PUBLIC;
