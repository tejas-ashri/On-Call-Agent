-- schema.sql — the relational model of record (doc 02 §2.1). Idempotent: drops then
-- recreates every table, so a test run always starts from a known-clean schema.
--
-- Faithful to doc 02 §2.1 with two pragmatic, documented adaptations for a runnable
-- single-node dev DB:
--   1. Tables are created parent-first (autonomy_grants before actions) so forward FK
--      references resolve; the doc lists them in narrative order.
--   2. `events` is LIST-partitioned by tenant_id exactly as designed; we add the DEFAULT
--      partition the doc itself describes (§5.1) so inserts work without per-tenant
--      partition management in dev.
-- IDs are stored as `uuid` (doc 00 §4): the app's ULIDs render to the prefixed strings
-- (inc_…, evt_…) while the stored 128-bit value is a canonical uuid.

DROP TABLE IF EXISTS
    webhook_inbox, event_outbox, runbooks, usage_meter, audit_log, action_audit,
    actions, autonomy_grants, incident_events, incidents, events, services,
    tenant_integrations, tenants
    CASCADE;

-- Global (NOT tenant-scoped): the only table outside RLS. Holds routing + plan.
CREATE TABLE tenants (
    tenant_id      uuid PRIMARY KEY,
    name           text NOT NULL,
    tenancy_model  text NOT NULL DEFAULT 'pool'
                     CHECK (tenancy_model IN ('pool','bridge','silo')),
    region         text NOT NULL DEFAULT 'us',
    plan           text NOT NULL DEFAULT 'trial',
    cost_ceiling_usd numeric(10,2) NOT NULL DEFAULT 50.00,
    status         text NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active','suspended','closed')),
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE tenant_integrations (
    integration_id uuid PRIMARY KEY,
    tenant_id      uuid NOT NULL REFERENCES tenants(tenant_id),
    provider       text NOT NULL,
    mode           text NOT NULL CHECK (mode IN ('agentless','agent')),
    vault_path     text NOT NULL,
    scopes         text[] NOT NULL DEFAULT '{}',
    status         text NOT NULL DEFAULT 'connected'
                     CHECK (status IN ('connected','degraded','revoked')),
    connected_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, provider, integration_id)
);
CREATE INDEX ix_integrations_tenant ON tenant_integrations (tenant_id, provider);

CREATE TABLE services (
    service_id     uuid PRIMARY KEY,
    tenant_id      uuid NOT NULL REFERENCES tenants(tenant_id),
    name           text NOT NULL,
    kind           text NOT NULL DEFAULT 'web'
                     CHECK (kind IN ('web','worker','cron','db','edge','external')),
    topology       jsonb NOT NULL DEFAULT '{}',
    discovered_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, name)
);
CREATE INDEX ix_services_tenant ON services (tenant_id);

-- Persisted CanonicalEvent. Partitioned LIST(tenant_id) → RANGE(occurred_at) (§5.1).
CREATE TABLE events (
    event_id       uuid NOT NULL,
    tenant_id      uuid NOT NULL,
    integration_id uuid NOT NULL,
    source         text NOT NULL,
    kind           text NOT NULL
                     CHECK (kind IN ('alert','log','metric','deploy','error','trace','ping')),
    service_id     uuid,
    severity       text NOT NULL
                     CHECK (severity IN ('debug','info','warning','error','critical')),
    title          text NOT NULL,
    body_redacted  text NOT NULL,
    fingerprint    text NOT NULL,
    labels         jsonb NOT NULL DEFAULT '{}',
    redaction_meta jsonb NOT NULL DEFAULT '{}',
    raw_ref        text,
    occurred_at    timestamptz NOT NULL,
    received_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, occurred_at, event_id)
) PARTITION BY LIST (tenant_id);
-- DEFAULT partition: the doc's catch-all (§5.1). In prod a per-tenant LIST partition is
-- created on first event; in dev this keeps INSERTs working without that machinery.
CREATE TABLE events_default PARTITION OF events DEFAULT;

-- Incident PROJECTION (ADR-011). Source of truth is incident_events.
CREATE TABLE incidents (
    incident_id     uuid PRIMARY KEY,
    tenant_id       uuid NOT NULL REFERENCES tenants(tenant_id),
    primary_service_id uuid REFERENCES services(service_id),
    state           text NOT NULL DEFAULT 'OPEN'
                      CHECK (state IN ('OPEN','TRIAGING','DIAGNOSED','AWAITING_APPROVAL',
                                       'REMEDIATING','MONITORING','ESCALATED','RESOLVED','CLOSED')),
    confidence_band text CHECK (confidence_band IN ('LOW','MEDIUM','HIGH')),
    title           text,
    root_cause      text,
    checkpoint_ref  text,
    version         int NOT NULL DEFAULT 0,
    opened_at       timestamptz NOT NULL DEFAULT now(),
    closed_at       timestamptz,
    UNIQUE (tenant_id, incident_id)
);
CREATE INDEX ix_incidents_tenant_state ON incidents (tenant_id, state)
    WHERE state NOT IN ('RESOLVED','CLOSED');

-- Append-only event store for incidents (ADR-011, P8). NEVER updated/deleted.
CREATE TABLE incident_events (
    incident_event_id uuid NOT NULL,
    tenant_id         uuid NOT NULL,
    incident_id       uuid NOT NULL,
    seq               bigint NOT NULL,
    type              text NOT NULL,
    payload           jsonb NOT NULL DEFAULT '{}',
    actor             text NOT NULL,
    occurred_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, incident_id, seq)
);

CREATE TABLE autonomy_grants (
    grant_id     uuid PRIMARY KEY,
    tenant_id    uuid NOT NULL REFERENCES tenants(tenant_id),
    action_type  text NOT NULL,
    max_tier     text NOT NULL DEFAULT 'T0'
                   CHECK (max_tier IN ('T0','T1','T2','T3')),
    constraints  jsonb NOT NULL DEFAULT '{}',
    granted_by   text NOT NULL,
    granted_at   timestamptz NOT NULL DEFAULT now(),
    revoked_at   timestamptz
);
CREATE UNIQUE INDEX uq_active_grant
    ON autonomy_grants (tenant_id, action_type)
    WHERE revoked_at IS NULL;

CREATE TABLE actions (
    action_id     uuid PRIMARY KEY,
    tenant_id     uuid NOT NULL REFERENCES tenants(tenant_id),
    incident_id   uuid NOT NULL REFERENCES incidents(incident_id),
    grant_id      uuid REFERENCES autonomy_grants(grant_id),
    action_type   text NOT NULL,
    target        jsonb NOT NULL,
    params        jsonb NOT NULL DEFAULT '{}',
    reversible    boolean NOT NULL,
    proposed_tier text NOT NULL CHECK (proposed_tier IN ('T0','T1','T2','T3')),
    required_tier text NOT NULL CHECK (required_tier IN ('T0','T1','T2','T3')),
    status        text NOT NULL DEFAULT 'proposed'
                    CHECK (status IN ('proposed','awaiting_approval','approved',
                                      'executing','succeeded','failed','rolled_back')),
    approved_by   text,
    executed_at   timestamptz,
    rollback_ref  text,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_actions_incident ON actions (tenant_id, incident_id);

CREATE TABLE action_audit (
    action_audit_id uuid PRIMARY KEY,
    tenant_id       uuid NOT NULL REFERENCES tenants(tenant_id),
    action_id       uuid NOT NULL REFERENCES actions(action_id),
    from_status     text,
    to_status       text NOT NULL,
    reason          text,
    actor           text NOT NULL,
    at              timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_action_audit_action ON action_audit (tenant_id, action_id, at);

-- Tamper-evident audit log. This doc owns the table; doc 05 owns the hash-chain.
CREATE TABLE audit_log (
    audit_id    uuid PRIMARY KEY,
    tenant_id   uuid NOT NULL REFERENCES tenants(tenant_id),
    seq         bigint NOT NULL,
    actor       text NOT NULL,
    action      text NOT NULL,
    target      text,
    detail      jsonb NOT NULL DEFAULT '{}',
    entry_hash  bytea NOT NULL,
    prev_hash   bytea NOT NULL,
    at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, seq)
);

-- Usage events for billing. This doc owns the table; doc 07 owns billing logic.
CREATE TABLE usage_meter (
    usage_id        uuid PRIMARY KEY,
    tenant_id       uuid NOT NULL REFERENCES tenants(tenant_id),
    incident_id     uuid,
    metric          text NOT NULL
                      CHECK (metric IN ('llm_tokens_in','llm_tokens_out','tool_call',
                                        'incident','event_ingested','action_executed')),
    quantity        numeric(18,4) NOT NULL,
    unit_cost_usd   numeric(12,6),
    model           text,
    idempotency_key text NOT NULL,
    occurred_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, idempotency_key)
);
CREATE INDEX ix_usage_tenant_time ON usage_meter (tenant_id, occurred_at);

CREATE TABLE runbooks (
    runbook_id    uuid PRIMARY KEY,
    tenant_id     uuid NOT NULL REFERENCES tenants(tenant_id),
    service_id    uuid REFERENCES services(service_id),
    title         text NOT NULL,
    body_redacted text NOT NULL,
    source        text NOT NULL DEFAULT 'scribe'
                    CHECK (source IN ('scribe','human','imported')),
    embedding_ref text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_runbooks_tenant_service ON runbooks (tenant_id, service_id);

-- Ingestion-edge operational tables (doc 03 §3): the inbox/outbox idempotency pair.
-- Tenant-scoped and RLS-governed like the rest; doc 02 §2.1 notes they live with the
-- connector but lists them here so the schema-of-record is complete.
CREATE TABLE webhook_inbox (
    inbox_id        uuid PRIMARY KEY,
    tenant_id       uuid NOT NULL REFERENCES tenants(tenant_id),
    integration_id  uuid NOT NULL,
    idempotency_key text NOT NULL,
    received_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (integration_id, idempotency_key)   -- a redelivery hits this and is deduped
);
CREATE INDEX ix_inbox_tenant ON webhook_inbox (tenant_id, received_at);

CREATE TABLE event_outbox (
    outbox_id    uuid PRIMARY KEY,
    tenant_id    uuid NOT NULL REFERENCES tenants(tenant_id),
    payload      jsonb NOT NULL,                -- the redacted CanonicalEvent to relay
    status       text NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending','relayed')),
    created_at   timestamptz NOT NULL DEFAULT now(),
    relayed_at   timestamptz
);
CREATE INDEX ix_outbox_pending ON event_outbox (tenant_id, created_at) WHERE status = 'pending';
