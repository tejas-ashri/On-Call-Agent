-- grants.sql — which *verbs* the app role may use (doc 02 §3.3). Applied always, after
-- schema and independently of RLS: grants decide WHICH STATEMENTS are allowed, RLS
-- (policies.sql) decides WHICH ROWS they see. Keeping them separate is what lets the
-- isolation test run with access but no row-filtering (the RED step).
--
-- Append-only tables get no UPDATE/DELETE; combined with FORCE RLS that is
-- belt-and-suspenders against history rewrites (P8).
GRANT SELECT                  ON tenants                                          TO sentinel_app;
GRANT SELECT, INSERT          ON events, incident_events, action_audit, audit_log TO sentinel_app;
GRANT SELECT, INSERT, UPDATE  ON incidents, actions, autonomy_grants, services,
                                 tenant_integrations, runbooks                    TO sentinel_app;
GRANT SELECT, INSERT          ON usage_meter, webhook_inbox                       TO sentinel_app;
-- event_outbox: the relay marks rows relayed, so it needs UPDATE.
GRANT SELECT, INSERT, UPDATE  ON event_outbox                                      TO sentinel_app;
