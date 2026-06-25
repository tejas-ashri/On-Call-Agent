# 00 — Glossary & Design Decisions (The Constitution)

> **Status:** Canonical. Every other document in this set defers to this one for terminology, identifiers, data-contract shapes, the autonomy model, and the architecture decisions recorded here. If a companion doc appears to contradict this file, this file wins and the companion is wrong.

This document exists so that a reader — or a contributor, or a subagent writing one of the companion docs — can align on *exactly what words mean* and *which decisions are already settled* before reading any deep-dive. It is deliberately terse where the companion docs are deep. Think of it as the project's shared memory.

---

## 1. The three inviolable constraints

Everything in Sentinel is downstream of one fact: **we operate against other people's production infrastructure.** Three constraints fall out, and they are not negotiable. Every design decision must be traceable to at least one.

1. **C1 — Never be the cause of an outage.** Read-heavy by default. Every write (rollback, restart, scale, config change) is opt-in, allow-listed, reversible, and approval-gated until a tenant explicitly grants standing autonomy for that specific action type.
2. **C2 — Minimize what we can see.** Founders will not hand prod credentials to a startup. Integration is agentless where possible, least-privilege always, and architected so logs/secrets are redacted *before* they reach the control plane.
3. **C3 — Onboard in minutes, not days.** Time-to-first-value is the product's life-or-death metric. If setup takes a weekend, a solo founder never finishes.

When a companion doc justifies a decision, it should cite the constraint(s) it serves, e.g. "(C1, C2)".

---

## 2. Derived design principles

These are the operational rules the constraints imply. They are referenceable by number (P1…P10).

- **P1 — Multi-tenancy from byte one.** Every event, row, vector, trace, lock, budget, and credential is tenant-scoped. This is never retrofitted. (C1, C2)
- **P2 — Isolation as defense in depth.** Tenant isolation is enforced at *three independent layers* — database (Postgres RLS), memory (Qdrant per-tenant namespaces), and credentials (JIT-brokered, scoped, short-lived) — so that a bug in any one layer is not sufficient to cross tenants. (C2)
- **P3 — Redact at the edge, not at rest.** Secrets and PII are stripped at the ingestion boundary before persistence, so the durable stores never hold them. (C2)
- **P4 — Outbound-only connectivity.** Sentinel never requires an inbound port into a tenant network. The tenant agent dials out; provider data is pulled. (C1, C3)
- **P5 — Graduated autonomy, tenant-controlled.** Trust is a ladder the tenant climbs deliberately, per action type, not a switch. Default is observe-only. (C1)
- **P6 — Confidence is calibrated, not vibes.** Any autonomous action is gated on a *calibrated* confidence score (validated against the eval set), never a raw model probability. (C1)
- **P7 — Everything metered, everything bounded.** Every LLM call, tool call, and incident is metered per tenant, and every tenant has a hard cost ceiling enforced by a circuit breaker. (margins; C1 via blast-radius limits)
- **P8 — Durable, event-sourced incidents.** Incident state is an append-only event log; the live view is a projection. Incidents survive worker restarts and are fully auditable and replayable. (C1, audit)
- **P9 — Least privilege, just in time.** No long-lived tenant credential ever lives at rest inside a worker or agent. Credentials are minted scoped, used once, and never logged. (C2)
- **P10 — Privacy-preserving evaluation.** The eval pipeline operates only on redacted traces. Improving the product never requires an engineer to see a tenant's secret. (C2)

---

## 3. Canonical glossary

**Control plane.** Sentinel's multi-tenant SaaS (the cloud side). Houses ingestion, correlation, orchestration, the MCP router, state stores, billing, and observability. Tenants never get an inbound connection *into* their network from here.

**Tenant plane.** The tenant's own infrastructure. Connects to the control plane outbound-only, via either the **agent** or **agentless** mode.

**Tenant.** A paying (or trial) customer account — a founder or small team. The unit of isolation. Identified by `tenant_id`.

**Sentinel Agent ("the agent").** A single lightweight container (Go or Rust) a tenant optionally deploys (sidecar/daemonset). Redacts and forwards telemetry, and executes approved write actions. Connects via outbound mTLS tunnel; opens **no inbound ports**. Distinct from the LLM "agents" below — when ambiguity is possible, write **Sentinel Agent** vs. **LLM agent / specialist**.

**Agentless mode.** Integration via read-only, least-privilege OAuth tokens / API keys to the tenant's PaaS and observability providers (Vercel, Sentry, Fly, Railway, Render, Supabase/Neon, GitHub, Cloudflare, Grafana Cloud, UptimeRobot/BetterStack…). The default, friction-minimizing onboarding path. (C3)

**Connector / Adapter.** A provider-specific integration module that normalizes a provider's alerts, logs, metrics, deploys, and errors into the **Canonical Event** schema. "Connector" = the whole integration; "adapter" = the normalization code inside it.

**Ingestion edge.** Regional gateway that terminates agent tunnels and provider webhooks, authenticates the tenant, **redacts** (P3), and produces to Kafka.

**Canonical Event.** The normalized telemetry envelope every source is mapped into. Defined in §5.1. The lingua franca of the system.

**Correlator.** Collapses storms of related events into a single **Incident** using per-tenant dedup windows, topology, and learned baselines.

**Incident.** The central work item: a correlated cluster of events representing one real-world problem. Event-sourced (P8). Lifecycle in §5.3.

**Orchestrator.** The LangGraph runtime. Spawns one tenant-scoped **incident graph** per incident.

**Incident Commander ("Commander").** The orchestrating LLM agent that dispatches specialists, synthesizes hypotheses, reflects, and decides escalation.

**Specialist.** A focused LLM agent the Commander dispatches: **Log**, **Metric**, **Trace**, **Change/Deploy**, **Error-tracking**. Run in parallel.

**Planner.** The LLM agent that drafts remediation — a PR, a patch, or a suggested command — once a root cause is hypothesized.

**Scribe.** The LLM agent that writes the postmortem and updates tenant memory after resolution.

**MCP Router.** The security spine. A routing layer in front of per-integration MCP servers that injects the *correct tenant's* JIT credentials into each tool call and makes cross-tenant tool access physically impossible. See ADR-005.

**JIT credential.** A short-lived, narrowly-scoped credential minted on demand from Vault for a single tool call, never persisted in the worker, never logged. (P9)

**Autonomy grant.** A tenant-issued, per-action-type permission specifying the highest **autonomy tier** (§6) Sentinel may use for that action type. Default: T0.

**Graduated escalation.** The product-defining behavior: based on calibrated confidence × autonomy grant × reversibility, Sentinel either acts-and-reports, or pages a human **with the full diagnosis and a drafted fix already attached.** (§6)

**Redaction.** Edge-side stripping of secrets/PII via layered detectors (regex + entropy + small classifier) before persistence. (P3)

**Trust dashboard.** The tenant-facing surface showing autonomous-resolution rate, page count, and median time-to-diagnosis. Doubles as an eval surface (see doc 06).

**Golden incident library.** A versioned corpus of replayable incidents spanning indie stacks, used for regression-gated evaluation in CI.

**Pool / Bridge / Silo.** The three multi-tenancy data models (shared DB+RLS / shared DB+per-tenant schema / dedicated DB). Sentinel is **pool-first, silo-on-graduation**. See ADR-001.

---

## 4. Naming & identifier conventions

**Identifiers.** Prefixed, k-sortable ULIDs rendered as strings; the underlying column type is `uuid` (a ULID is a valid 128-bit value storable as `uuid`). Prefixes exist for human/log readability:

| Entity | Prefix | Example |
|---|---|---|
| Tenant | `ten_` | `ten_01HZX9...` |
| Service (tenant's logical service) | `svc_` | `svc_01HZ...` |
| Canonical event | `evt_` | `evt_01HZ...` |
| Incident | `inc_` | `inc_01HZ...` |
| Action (write request) | `act_` | `act_01HZ...` |
| Integration (connected provider instance) | `int_` | `int_01HZ...` |
| Autonomy grant | `grant_` | `grant_01HZ...` |

**Database.** `snake_case` tables and columns. Every tenant-scoped table carries `tenant_id uuid not null` as its first column and is governed by an RLS policy (ADR-001). Timestamps are `timestamptz`, named `*_at`, UTC.

**Kafka topics.** Dotted lowercase by domain: `evt.normalized`, `incident.events`, `action.requested`, `action.audit`, `metering.usage`. **Partition key is always `tenant_id`** (see ADR-007 for fair-share consumption). Regional prefix where residency requires it: `eu.evt.normalized`.

**Services (control-plane deployables).** `ingestion-edge`, `correlator`, `orchestrator`, `mcp-router`, `connector-<provider>` (e.g. `connector-sentry`), `metering`, `billing`, `onboarding`, `notifier`, `api` (the dashboard backend).

**Vault paths.** `secret/data/tenants/<tenant_id>/integrations/<provider>` for stored tenant credentials; transit/PKI mounts issue the JIT scoped creds.

**Autonomy tiers.** Always written `T0`–`T3` (§6). Confidence bands: `LOW`, `MEDIUM`, `HIGH` (§6).

---

## 5. Canonical data contracts (shapes)

These are the *canonical shapes*. Full schemas (columns, indexes, RLS SQL, Pydantic models) live in doc 02 (data) and doc 04 (agent contracts); those docs MUST use these field names verbatim.

### 5.1 `CanonicalEvent`
The normalized envelope every connector emits. Fields:

`event_id` · `tenant_id` · `integration_id` · `source` (provider enum) · `kind` (`alert`|`log`|`metric`|`deploy`|`error`|`trace`|`ping`) · `service` (`svc_…`, resolved via discovery) · `severity` (`debug`|`info`|`warning`|`error`|`critical`) · `title` · `body_redacted` (text, post-redaction) · `fingerprint` (stable hash for dedup) · `labels` (string→string map) · `occurred_at` · `received_at` · `redaction_meta` (counts/types of redactions applied) · `raw_ref` (pointer to the redacted raw blob in object storage; nullable).

### 5.2 `AutonomyGrant`
`grant_id` · `tenant_id` · `action_type` (e.g. `deploy.rollback`, `process.restart`, `service.scale`) · `max_tier` (`T0`–`T3`) · `constraints` (e.g. allow-listed services, max replicas, time windows) · `granted_by` · `granted_at` · `revoked_at` (nullable).

### 5.3 `Incident` lifecycle (states)
`OPEN` → `TRIAGING` → `DIAGNOSED` → { `AWAITING_APPROVAL` | `REMEDIATING` | `ESCALATED` } → `RESOLVED` → `CLOSED`. A `MONITORING` state may follow `REMEDIATING` before `RESOLVED`. All transitions are recorded as events in `incident.events` (P8). Terminal states: `RESOLVED`/`CLOSED`. `ESCALATED` means a human was paged with diagnosis + drafted fix.

### 5.4 `ActionRequest`
`action_id` · `tenant_id` · `incident_id` · `action_type` · `target` (service/resource) · `params` · `reversible` (bool) · `proposed_tier` · `required_tier` · `status` (`proposed`|`awaiting_approval`|`approved`|`executing`|`succeeded`|`failed`|`rolled_back`) · `approved_by` (nullable) · `executed_at` (nullable) · `rollback_ref` (nullable). Every state change is an immutable `action.audit` record.

---

## 6. The graduated-autonomy ladder (canonical)

Trust is a four-rung ladder. Each `(tenant × action_type)` pair has a `max_tier`. **Default for everything is T0.**

| Tier | Name | Behavior |
|---|---|---|
| **T0** | Observe | Read-only. Diagnose and page only. No writes, ever. |
| **T1** | Suggest | Drafts a fix (PR link / patch / command) but **never executes**. Human runs it. |
| **T2** | Approve-to-act | Will execute an allow-listed, **reversible** action, but only after explicit per-incident human approval (one tap from phone). |
| **T3** | Autonomous | Executes allow-listed, reversible, **high-confidence** actions automatically, then reports. Still bounded by policy, reversibility, and cost budget. |

**Confidence bands** (calibrated, P6): `LOW` < 0.5 ≤ `MEDIUM` < 0.8 ≤ `HIGH`.

**The escalation matrix** (the policy the Commander applies — full treatment in doc 04):

| Situation | Outcome |
|---|---|
| `HIGH` confidence + reversible + grant ≥ T3 | **Act autonomously, then report.** |
| `HIGH`/`MEDIUM` + reversible + grant = T2 | **Page with one-tap approval** + drafted fix. |
| Any confidence + grant ≤ T1 | **Page with full diagnosis + drafted fix.** Never act. |
| Irreversible action, any tier | **Always page.** Never auto-execute, even at T3. |
| `LOW` confidence, any tier | **Page**, present evidence and competing hypotheses; do not act. |

The page always carries: suspected root cause, the evidence chain, competing hypotheses, and a drafted remediation. The human approves in ~15 seconds instead of starting from zero. **This ladder is the product.**

---

## 7. Canonical technology stack

| Layer | Choice | Notes |
|---|---|---|
| Language (control plane) | Python 3.12, fully typed, Pydantic v2 | Agent ecosystem |
| Agent framework | LangGraph | Checkpointing + HITL interrupts + durable state |
| LLMs | Tiered: cheap model for specialists, strong model for Commander/Planner; per-tenant routing | Margin control (ADR-008) |
| Messaging | Kafka, `tenant_id`-keyed partitions | Multi-tenant streaming + replay (ADR-007) |
| OLTP / event store | Postgres 16 + Row-Level Security | Isolation as a DB guarantee (ADR-001) |
| Vector DB | Qdrant, per-tenant namespaces/collections | Isolated memory (P2) |
| Cache / locks / budgets | Redis | Locks, quotas, cost ceilings |
| Tool protocol | MCP + JIT credential-brokering router | The security spine (ADR-005) |
| Secrets | Vault (or cloud KMS) + envelope encryption | Per-tenant credential isolation (P9) |
| Tenant agent | Single Go/Rust container, outbound-only mTLS | Tiny footprint, no inbound ports (P4) |
| Edge | Regional gateways (Cloud Run / Fly) | Low-latency tunnel termination, residency |
| LLM observability | Langfuse, self-hosted | Per-tenant traces + evals + cost |
| Billing | Stripe + internal metering service | Usage + subscription (doc 07) |
| Frontend | Next.js dashboard + Slack/SMS/voice paging | Where founders live |
| IaC | Terraform | Reproducible multi-region infra |
| CI/CD | GitHub Actions → ArgoCD (GitOps) | Eval-gated, canaried (doc 06) |

---

## 8. Architecture Decision Records

Each ADR is **Context → Decision → Consequences**. They are the settled calls; companion docs implement them, they do not relitigate them.

**ADR-001 — Tenant isolation enforced at the database via Postgres RLS; pool-first, silo-on-graduation.**
*Context:* A single application bug must not be able to leak one tenant's prod data into another's. *Decision:* Every tenant-scoped table has `tenant_id` and an RLS policy keyed to a per-request session variable; the app connects as a role that *cannot* bypass RLS. Start in **pool** (shared DB), graduate large/sensitive tenants to **silo** (dedicated DB/schema) via a bridge step. *Consequences:* Cross-tenant reads are impossible even with a buggy query (defense in depth, P2); a mandatory CI test proves tenant A cannot read tenant B; a small per-query cost to set the session GUC. (C1, C2)

**ADR-002 — Outbound-only connectivity; agentless-first, lightweight agent second.**
*Context:* "Open a firewall port for a startup" is a deal-killer; weekend-long setup loses solo founders. *Decision:* Two modes, both outbound-only: agentless OAuth/read-key integrations for 10-minute onboarding (C3), and an optional single-container agent that dials out over mTLS for deeper/write-capable access. *Consequences:* No inbound attack surface into tenant networks (P4); the connector library becomes the moat (doc 03); write actions require the agent or a provider write-API + explicit grant.

**ADR-003 — Read-only by default; writes are opt-in, allow-listed, reversible, approval-gated.**
*Context:* C1. *Decision:* The graduated-autonomy ladder (§6). Nothing writes until a tenant grants a tier for a specific action type; irreversible actions always page. *Consequences:* Sentinel is safe to try; trust is earned incrementally; the policy engine and audit log become first-class (doc 04, doc 05).

**ADR-004 — Redact at the edge, before persistence.**
*Context:* C2 — durable stores must never hold tenant secrets. *Decision:* Layered redaction (regex + entropy + small classifier) runs at the ingestion edge; only `body_redacted` is persisted; `redaction_meta` records what was stripped. *Consequences:* Eval, storage, and backups are all secret-free by construction (P3, P10); some recall cost (over-redaction) accepted and tuned.

**ADR-005 — JIT credential brokering via the MCP Router; no long-lived tenant keys at rest in workers.**
*Context:* A tenant-A incident graph must be *physically* unable to acquire tenant-B's tools (the confused-deputy problem). *Decision:* All tool calls route through the MCP Router, which mints a short-lived, scoped credential from Vault per call, injects it, and never logs it; the LLM agent never holds a durable key. *Consequences:* Blast radius of a compromised worker is one short-lived scoped token (P9); full per-tenant audit; router is a critical-path component requiring its own HA + tests (doc 05).

**ADR-006 — LangGraph for orchestration.**
*Context:* Incidents are long-running, must survive restarts, and must pause for human approval mid-flight. *Decision:* Model each incident as a checkpointed LangGraph with first-class `interrupt` for human-in-the-loop. *Consequences:* Durable, resumable incidents (P8); HITL is a graph state, not a hack; vendor/runtime coupling accepted.

**ADR-007 — Tenant-keyed Kafka partitions + per-tenant fair-share consumption.**
*Context:* One tenant's alert storm must not starve another's incident handling (noisy neighbor). *Decision:* Partition by `tenant_id`; consumers apply per-tenant fair-share scheduling and per-tenant quotas; workers autoscale on consumer lag (KEDA). *Consequences:* Isolation under load; replayability; partition-skew for whales handled by graduation/sub-keying.

**ADR-008 — Tiered LLM routing with calibrated confidence.**
*Context:* Margins (P7) and trust (P6). *Decision:* Cheap models run specialists; a strong model runs Commander/Planner; routing is per-tenant-configurable. Confidence used for autonomy is calibrated against the eval set, not raw logprobs. *Consequences:* Predictable unit economics (doc 07); calibration becomes an eval responsibility (doc 06).

**ADR-009 — Canonical event schema; normalize at ingestion.**
*Context:* Dozens of heterogeneous providers. *Decision:* Every connector maps to `CanonicalEvent` (§5.1) at ingestion; the rest of the system speaks only canonical. *Consequences:* Adding a provider is an adapter, not a system change (doc 03); downstream logic is provider-agnostic; mapping fidelity is a per-connector test surface.

**ADR-010 — Per-tenant cost ceilings + idempotent metering as first-class governance.**
*Context:* A tenant whose prod generates 500 incidents/day must not destroy margins or, worse, run unbounded autonomous actions. *Decision:* Meter every LLM/tool/incident per tenant idempotently; enforce a hard monthly ceiling via a circuit breaker that degrades to T0 + page when tripped. *Consequences:* Gross margin per tenant is knowable and defensible (doc 07); retries never double-charge; cost is also a safety control (caps blast radius).

**ADR-011 — Event-sourced incidents.**
*Context:* Auditability, restart-survival, and replayable evals. *Decision:* `incident.events` is the append-only source of truth; the `incidents` row is a projection. *Consequences:* Full audit trail and time-travel; golden-incident replay is natural (doc 06); projection/rebuild logic required.

**ADR-012 — Privacy-preserving evaluation on redacted traces only.**
*Context:* C2/P10 — improving the product must never require seeing a tenant's secret. *Decision:* The eval pipeline (golden library + production sampling) consumes only redacted traces; no un-redacted data path exists into evals. *Consequences:* Safe-by-construction evaluation; some signal loss from redaction accepted; redaction quality becomes doubly important.

---

## 9. Document map

| Doc | Title |
|---|---|
| `README.md` | Master overview & index |
| `00-glossary-and-decisions.md` | **This file** — the constitution |
| `01-architecture.md` | System architecture & data flow |
| `02-data-model-and-isolation.md` | Data model, multi-tenancy & isolation |
| `03-connectors-and-integrations.md` | Connector layer & integration modes |
| `04-agent-orchestration.md` | Agent orchestration & graduated autonomy |
| `05-security-and-trust.md` | Security, trust & threat model |
| `06-evaluation-and-observability.md` | Evaluation, observability & CI/CD |
| `07-billing-and-unit-economics.md` | Billing, metering & unit economics |
| `08-business-gtm-roadmap-risk.md` | Business, GTM, roadmap & risk |
