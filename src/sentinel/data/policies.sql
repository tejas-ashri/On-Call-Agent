-- policies.sql — Row-Level Security, the centerpiece (doc 02 §3.3). Applied AFTER
-- schema.sql. Until this runs, the tables are wide open to the app role — which is the
-- whole point of the RED step in the isolation test: prove the test catches a breach
-- BEFORE RLS, then watch RLS close it.
--
-- For each tenant-scoped table: ENABLE + FORCE RLS (FORCE binds even the table owner),
-- then the canonical policy. USING governs visibility (SELECT/UPDATE/DELETE); WITH CHECK
-- governs writes (INSERT/UPDATE) — you can neither SEE nor WRITE a row whose tenant_id
-- differs from the session's app.tenant_id GUC. current_setting(..., true) returns NULL
-- when the GUC is unset, and NULL matches nothing → an unscoped connection is fail-closed.

DO $$
DECLARE
    t text;
    scoped text[] := ARRAY[
        'tenant_integrations','services','events','incidents','incident_events',
        'actions','action_audit','autonomy_grants','audit_log','usage_meter','runbooks',
        'webhook_inbox','event_outbox'
    ];
BEGIN
    FOREACH t IN ARRAY scoped LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE  ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I '
            'USING      (tenant_id = current_setting(''app.tenant_id'', true)::uuid) '
            'WITH CHECK (tenant_id = current_setting(''app.tenant_id'', true)::uuid)', t);
    END LOOP;
END $$;
