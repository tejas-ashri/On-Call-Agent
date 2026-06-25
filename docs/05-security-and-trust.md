# 05 — Security, Trust & Threat Model
*The security spine: an MCP Router that mints short-lived, tenant-scoped JIT credentials per tool call so tenant-A's incident graph physically cannot touch tenant-B's prod, edge-side redaction so the durable stores never hold a secret, and a tamper-evident per-tenant audit chain — engineered so Sentinel is provably safer than a founder SSHing into prod half-asleep.*

This document owns the trust posture: **credential brokering** (ADR-005), **redaction internals** (ADR-004), the **threat model**, the **audit-log hash-chain mechanism** over doc 02's `audit_log` table, and the **SOC2** mapping. It realizes **C2** (minimize what we can see) and the security half of **C1**, built on **P2** (defense in depth), **P3** (redact at the edge), **P9** (least privilege, JIT), and **P10**. It defers: tiers `T0`–`T3` and `EscalationDecision` to doc 00 §6 / doc 04; the `audit_log` *table* and RLS to doc 02; the agent mTLS transport and `WriteCapability`/`ActionType` to doc 03; edge/router placement to doc 01. It implements ADR-004 and ADR-005; it relitigates no ADR.

The thesis: **every other security control is a fence; the MCP Router is the property line.** Where doc 02 makes a cross-tenant *read* physically impossible at the database, this doc makes a cross-tenant *tool call* physically impossible at the credential broker — the same P2 argument, one layer out.

---

## 1. The trust boundary, drawn for security

Doc 01 draws the planes; here we draw them as *trust zones* and mark where authority is granted, narrowed, and spent. The single security-relevant fact is that **authority flows in exactly one direction and is narrowed at every hop** — never widened.

```mermaid
flowchart TB
  subgraph Z0["ZONE 0 — Tenant plane (UNTRUSTED by us; we are a guest)"]
    PROV["Provider APIs<br/>read-scoped OAuth"]
    AGENT["Sentinel Agent<br/>mTLS client identity only<br/>(no durable prod key — P9)"]
  end

  subgraph Z1["ZONE 1 — Ingestion edge (semi-trusted, stateless, regional)"]
    GW["Gateway: authenticate<br/>(mTLS cert / webhook HMAC)"]
    RDX["Redaction pipeline<br/>(regex → entropy → PII classifier)<br/>ADR-004 / P3"]
  end

  subgraph Z2["ZONE 2 — Compute core (trusted code, ZERO durable secrets)"]
    ORC["orchestrator<br/>tenant-bound incident graph<br/>holds NO credential"]
    MCP["mcp-router<br/>★ the property line ★<br/>mints + injects JIT creds"]
  end

  subgraph Z3["ZONE 3 — Governance plane (the crown jewels)"]
    VLT[("Vault<br/>transit / PKI / KV-v2<br/>per-tenant paths")]
    PG[("Postgres + RLS<br/>audit_log hash-chain")]
  end

  PROV ==>|"raw telemetry (pull)"| GW
  AGENT ==>|"outbound mTLS (dial-out)"| GW
  GW --> RDX
  RDX ==>|"body_redacted only — secrets stripped"| ORC
  ORC -->|"tool_call intent (tenant-bound)"| MCP
  MCP ==>|"mint scoped, short-TTL cred"| VLT
  MCP ==>|"inject cred → provider / agent tunnel"| Z0
  MCP -->|"append (cred NEVER in payload)"| PG

  classDef z0 fill:#fdf2f2,stroke:#c0392b,stroke-width:2px;
  classDef z1 fill:#fef9e7,stroke:#b9770e,stroke-width:1px;
  classDef z2 fill:#f2f7fd,stroke:#2471a3,stroke-width:1px;
  classDef z3 fill:#eafaf1,stroke:#1e8449,stroke-width:2px;
  class Z0,PROV,AGENT z0;
  class Z1,GW,RDX z1;
  class Z2,ORC,MCP z2;
  class Z3,VLT,PG z3;
```

Three invariants the diagram encodes, each load-bearing:

1. **Secrets die at Zone 1.** Raw telemetry crosses Zone 0 → edge; only `body_redacted` crosses edge → Zone 2. The compute core — where the LLM agents live — **never sees an un-redacted byte** (P3). So a prompt-injection or a leaked LLM transcript cannot exfiltrate a tenant secret: there is no secret in the trace to leak.
2. **No durable secret lives in Zone 2.** The orchestrator and LLM agents hold zero standing credentials (P9); the *only* component that ever holds a tenant credential is the MCP Router, for milliseconds.
3. **The crown jewels (Zone 3) are reachable only by narrow, audited paths.** Vault only by the router (to mint), Postgres only through RLS-scoped sessions (doc 02). Authority is *minted* in Zone 3, *spent* against Zone 0, *recorded* back into Zone 3 — and only ever shrinks along the way.

---

## 2. The MCP Router & JIT credential brokering (ADR-005) ★

This is the security spine. It solves the **confused-deputy problem**: the router acts on behalf of many tenants, able to mint the authority of all of them, so a confused deputy is one *tricked* into using tenant-A's authority for tenant-B's request. Because tenant-B's request is partly shaped by an LLM reasoning over tenant-B's incident data, the threat is concrete — a prompt-injected or buggy LLM agent must not be able to name tenant-A's resources and have the router dutifully mint tenant-A's credential.

The router prevents this structurally, not by validation. Four mechanisms compose:

- **Tenant binding on the graph (P1).** Per doc 04 §1, every incident graph is constructed with a *frozen* `tenant_id` and a `ToolBroker` scoped to that tenant — a closure over its tenant. No code path, and crucially no *argument*, lets a running graph express "do this as a different tenant."
- **The scoped token derives from the request's tenant context, never from anything the LLM can influence** — the heart of the fix. The `tenant_id` selecting the Vault path comes from the **server-side `ToolBroker` identity**, fixed when the orchestrator spawned the graph from a `tenant_id`-keyed Kafka message (ADR-007). The LLM supplies *what* to do (`tool_calls` — doc 04's `SpecialistFinding`/`RemediationPlan`); never *whose credential*. Even if a finding said `"read logs for tenant ten_OTHER"`, that string is a tool argument, not an input to credential selection.
- **Server-side scoping.** Vault policy and lease TTL are computed server-side from `(tenant_id, action_type, target)`, clamped to least privilege (§3). The caller cannot request a broader scope; scope is a property of the request's *type*.
- **No ambient authority.** The router's own Vault AppRole can *only* mint per-tenant child tokens via templated policy — it cannot read any stored secret directly and holds no provider credential. There is no standing power for a deputy to misappropriate.

### 2.1 The brokering sequence (one tool call, end to end)

Trace a single Planner repo-read from tenant-A's incident graph:

```mermaid
sequenceDiagram
  autonumber
  participant G as Incident graph (tenant-A)<br/>frozen tenant_id
  participant B as ToolBroker (server-side,<br/>tenant-bound handle)
  participant R as MCP Router
  participant V as Vault (transit/PKI/KV)
  participant S as MCP server<br/>(connector-github)
  participant T as Tenant-A resource<br/>(provider API / agent tunnel)
  participant A as audit_log (Postgres)

  G->>B: tool_call(action="repo.read", target="svc_api", args)
  Note over B: tenant_id is from the CLOSURE,<br/>NOT from the LLM's args
  B->>R: BrokerRequest{tenant_id=ten_A, action, target, incident_id}
  R->>R: resolve least-priv scope + TTL from (action, target)
  R->>R: assert tenant_id == graph identity (defense in depth)
  R->>V: mint scoped cred at secret-path(ten_A, github)<br/>(child token / transit-signed / PKI leaf, TTL≤60s)
  V-->>R: short-lived credential (lease_id, TTL)
  R->>S: invoke tool WITH injected cred (in headers, not logs)
  S->>T: call provider/agent as tenant-A, least scope
  T-->>S: redacted result
  S-->>R: result (no credential echoed)
  R->>V: revoke lease (best-effort; TTL guarantees expiry anyway)
  R->>A: append audit entry {actor, action, target, lease_id, NO secret}
  R-->>B: result
  B-->>G: result
  Note over G,A: tenant-B's graph has a DIFFERENT closure →<br/>physically cannot construct a BrokerRequest for ten_A
```

The decisive frame is the `Note over B`: the `tenant_id` selecting the Vault path comes from the broker's closure, fixed at graph construction from the Kafka partition key — **before any LLM ran**. Tenant-B's graph is a different object with a different frozen `tenant_id`; it can emit any `tool_calls` it likes and still only mints against tenant-B's path. Cross-tenant tool access isn't *forbidden* — it's *unrepresentable*.

### 2.2 The router injecting credentials (code sketch)

The router keys credential minting on the **tenant-bound graph identity**, not on call arguments. The `BrokerRequest.tenant_id` is set by the `ToolBroker`, which is constructed per-graph and never mutated:

```python
# mcp_router/broker.py — Python 3.12, Pydantic v2. The security spine.
from __future__ import annotations
import contextlib
from datetime import timedelta
from pydantic import BaseModel, ConfigDict, Field


class BrokerRequest(BaseModel):
    """A request to spend authority. tenant_id is set SERVER-SIDE from the graph's
    frozen identity — never from LLM output. This is the confused-deputy fix."""
    model_config = ConfigDict(frozen=True)            # immutable once built
    tenant_id: str                                    # ten_… — from the ToolBroker closure
    incident_id: str
    provider: str                                     # 'github','vercel',… (CanonicalEvent.source)
    action: str                                       # tool verb, e.g. 'repo.read'
    target: str                                       # svc_… / resource — an ARGUMENT, not authority
    write: bool = False                               # write calls draw extra scrutiny + audit


class ToolBroker:
    """Constructed once per incident graph, closed over a FROZEN tenant_id (doc 04 §1).
    A graph holds exactly one of these; it is the only way the graph can reach a tool."""

    def __init__(self, *, tenant_id: str, router: "MCPRouter"):
        self._tenant_id = tenant_id          # frozen identity; no setter exists
        self._router = router

    async def call(self, *, provider: str, action: str, target: str,
                   write: bool = False, **args) -> "ToolResult":
        # tenant_id comes from SELF, not from args — args cannot carry authority.
        req = BrokerRequest(
            tenant_id=self._tenant_id, incident_id=self._router.incident_id_of(self),
            provider=provider, action=action, target=target, write=write,
        )
        return await self._router.broker(req, args)


class MCPRouter:
    def __init__(self, vault: "VaultClient", audit: "AuditChain", servers: dict[str, "MCPServer"]):
        self._vault, self._audit, self._servers = vault, audit, servers

    async def broker(self, req: BrokerRequest, args: dict) -> "ToolResult":
        # 1) Server-side least-privilege scope + short TTL, derived from (action, target).
        scope = scope_for(req.provider, req.action, req.target, write=req.write)  # §3.2
        ttl = timedelta(seconds=30 if not req.write else 60)

        # 2) Mint a scoped, short-lived credential from the TENANT's Vault path.
        #    The path is f"…/tenants/{req.tenant_id}/…" — tenant-B cannot reach ten_A's path
        #    because req.tenant_id is bound to the graph closure (confused-deputy fix).
        cred = await self._vault.mint_scoped(
            tenant_id=req.tenant_id, provider=req.provider, scope=scope, ttl=ttl,
        )
        try:
            server = self._servers[req.provider]
            # 3) Inject the credential into the tool invocation. It lives ONLY in this
            #    call's headers — never in args, never in logs, never in the LLM trace (C2).
            result = await server.invoke(
                action=req.action, target=req.target, args=args,
                credential=cred.secret,          # injected here, used once
            )
        finally:
            # 4) Revoke immediately (best-effort); the short TTL is the real guarantee (P9).
            with contextlib.suppress(Exception):
                await self._vault.revoke(cred.lease_id)
            # 5) Audit the spend — with the lease_id, NEVER the secret. Hash-chained (§5).
            await self._audit.append(
                tenant_id=req.tenant_id, actor=f"router:{req.incident_id}",
                action=f"tool.{req.action}", target=req.target,
                detail={"provider": req.provider, "lease_id": cred.lease_id,
                        "scope": scope, "write": req.write},   # redacted; no credential
            )
        return result
```

Two properties to underline. First, `cred.secret` exists only inside `broker()` — never returned to the graph, never in `args`, never serialized into the audit `detail`, and the logger redacts it even if a stack trace tries to print it. Second, `BrokerRequest` is `frozen=True` and `ToolBroker._tenant_id` has no setter: no in-process mutation can swap the tenant after construction. So the blast radius of a fully-compromised orchestrator worker is **one tenant's short-lived, scoped tokens for the duration of compromise** — not a standing credential, and never a *second* tenant's anything (ADR-005 consequence).

---

## 3. Secrets: Vault, envelope encryption & JIT issuance (P9)

### 3.1 What lives where (Vault path conventions, doc 00 §4)

Long-lived tenant credentials — OAuth refresh tokens, provider API keys (doc 03 §4) — live **only** in Vault KV-v2 at `secret/data/tenants/<tenant_id>/integrations/<provider>`, the canonical path from doc 00 §4. They are **envelope-encrypted**: Vault encrypts each secret under a per-tenant data key, itself wrapped by a key in Vault's `transit` mount (the KEK). Rotating the KEK re-wraps data keys without touching plaintext; a stolen Vault storage blob is inert without the transit master key (HSM/auto-unseal KMS, never on disk). **No long-lived tenant key is ever written to a worker's disk, env, or memory beyond a single call (P9)** — workers hold an AppRole token that can only *mint*, never *read* raw secrets.

JIT issuance uses two Vault mechanisms by auth model:

- **`transit`** where we hold a key and need a short-lived *derived* token or signed request — the key never leaves Vault; the router gets back only the ephemeral artifact.
- **`pki`** for the agent-tunnel and service-mesh mTLS leaves — short-TTL leaves from a per-region intermediate CA, auto-rotated before expiry (doc 03 §7's `rotate_before_expiry`).
- For OAuth providers, the KV refresh token is exchanged server-side for a **per-call access token** scoped to the single tool call, then discarded.

### 3.2 A Vault policy scoping a tenant path (HCL)

The router's AppRole is granted a *templated* policy so a single policy definition scopes every tenant by their identity, and a token minted for tenant-A can touch only tenant-A's path. Scope is decided server-side; the policy is the enforcement floor beneath it.

```hcl
# vault/policies/sentinel-router.hcl
# The MCP Router's AppRole binds an identity entity whose metadata carries the
# request's tenant_id (set server-side per broker call via a templated child token).
# {{identity.entity.metadata.tenant_id}} makes ONE policy isolate ALL tenants.

# Mint short-lived OAuth/derived creds — but ONLY under this tenant's integration path.
path "secret/data/tenants/{{identity.entity.metadata.tenant_id}}/integrations/*" {
  capabilities = ["read"]      # read the stored refresh token to exchange it; never list
}

# Transit: sign/derive a per-call token using THIS tenant's named key only.
path "transit/sign/tenant-{{identity.entity.metadata.tenant_id}}" {
  capabilities = ["update"]
}
path "transit/encrypt/tenant-{{identity.entity.metadata.tenant_id}}" {
  capabilities = ["update"]
}

# PKI: issue a short-TTL mTLS leaf for THIS tenant's agent tunnel only.
path "pki/issue/tenant-{{identity.entity.metadata.tenant_id}}" {
  capabilities = ["update"]
}

# Explicitly DENY the broad paths a confused deputy would need. Deny wins in Vault.
path "secret/data/tenants/*" {                 # cannot wildcard across tenants
  capabilities = ["deny"]
}
path "secret/metadata/tenants/*" {             # cannot even LIST who exists
  capabilities = ["deny"]
}
```

The templating is the second wall behind §2's application-layer binding: the child token for a broker call carries the bound tenant's identity metadata in its own claims, and Vault resolves `{{identity.entity.metadata.tenant_id}}` to *that* tenant — so a malformed request fails closed against Vault, not just against our code. Two independent layers (P2): app-side closure binding **and** Vault-side templated policy. The explicit `deny` on wildcards is belt-and-suspenders — in Vault `deny` overrides any `allow`, so no future policy edit can accidentally grant cross-tenant reach.

### 3.3 Rotation & revocation

- **JIT creds**: TTL ≤ 60s, revoked immediately after use (§2.2). Expiry — not revocation — is the guarantee, so a router crash mid-call still bounds exposure to the TTL.
- **Stored refresh tokens**: rotated by the connector reconciler ahead of `token_expires_at` (doc 03 §4); a provider `invalid_grant` is treated as revocation.
- **Transit KEK**: rotated on a schedule; envelope design means re-wrap is O(data keys), not O(secrets), and old ciphertext stays readable through `min_decryption_version` until re-encrypted.
- **mTLS / PKI leaves**: short TTL, auto-rotated before expiry; the intermediate CA is itself rotatable without re-bootstrapping agents within the offline-grace window.
- **Tenant offboarding**: deleting a tenant tombstones the KV path and disables the tenant's transit/PKI named keys, instantly rendering every past-issued artifact unrenewable (the deletion-saga gap is noted in §10 and doc 02).

---

## 4. Edge-side redaction (ADR-004, P3)

Redaction is *why* the durable stores never hold a secret, and *why* evaluation (doc 06, ADR-012/P10) runs on traces without an engineer seeing tenant data. It runs at the edge in the `authenticate → **redact** → normalize → produce` order (doc 01 §3), so even fields a connector copies verbatim into `body_redacted` are clean before Kafka. The connector's `normalize()` emits `body_redacted=""` as a placeholder (doc 03 §1.1); this stage fills it.

### 4.1 Why edge, not at rest

Redact *at rest* and the secret has already crossed the boundary, hit Kafka, possibly replicated, landed in backups — "we'll scrub it later" is not a posture a founder hands prod logs (C2). Redacting at the edge makes the durable systems (Postgres, Qdrant, object storage, Kafka, backups) **secret-free by construction**: no scrub job to forget, no window during which plaintext exists. The structural difference between "we delete secrets" and "we never store them."

### 4.2 Layered detectors & order of operations

Three layers run in series, cheapest-and-most-precise first — each layer's output is the next's input, so ordering changes both correctness and cost:

1. **Regex for known token formats (high precision).** Unambiguous shapes: AWS keys (`AKIA[0-9A-Z]{16}`), GitHub PATs (`ghp_…`), Stripe keys (`sk_live_…`), Google keys (`AIza…`), Slack tokens, PEM private-key blocks, JWTs, bearer headers, DSNs with embedded passwords. Near-zero false-positive; runs first so a known secret is *labeled by type* before entropy sees it — yielding a precise `redaction_meta` type, not a generic "high entropy."
2. **Shannon-entropy detection (catches the unknown).** Token-shaped substrings are scored by Shannon entropy over their character distribution; above a per-charset threshold and a minimum length, a string is treated as a probable secret even if it matches no known format — the net for the long tail (a bespoke key, a session id, a signed URL) regex can't enumerate. Runs *after* regex so labeled tokens aren't re-flagged; tuned toward **over-redaction** (§4.4).
3. **A small PII classifier (fuzzy, last).** A distilled NER-style model runs last on the residual, catching PII that is neither known-format nor high-entropy: emails, names, phones, IPs, free-text in stack frames and commit messages. Last because it is the priciest per token and least precise, so it sees the smallest surface.

Order matters beyond cost: a JWT is *also* high-entropy *and* contains PII-shaped claims. Regex-first labels it once, precisely, as `jwt` — avoiding a triple-count in `redaction_meta`.

### 4.3 The `redaction_meta` it emits + a worked example

The redactor replaces each detection with a typed, length-preserving-ish placeholder and emits `redaction_meta` — counts and types, never values — which is persisted alongside `body_redacted` on the `CanonicalEvent` (doc 00 §5.1, doc 02 `events.redaction_meta`). `redaction_meta` is itself safe to store and to show on the trust dashboard: it tells a tenant *how much* we stripped without revealing *what*.

```python
# edge/redaction.py — runs at the ingestion edge, BEFORE Kafka (ADR-004). Pure given config.
import math, re
from collections import Counter

_KNOWN = [  # (type, compiled pattern) — ordered, highest-precision first
    ("aws_access_key_id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("github_pat",        re.compile(r"\bghp_[A-Za-z0-9]{36}\b")),
    ("stripe_secret_key", re.compile(r"\b[rs]k_live_[A-Za-z0-9]{24,}\b")),
    ("jwt",               re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")),
    ("pg_conn_password",  re.compile(r"(?P<pre>postgres(?:ql)?://[^:]+:)[^@]+(?P<post>@)")),
    ("pem_private_key",   re.compile(r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----[\s\S]+?-----END[^-]+-----")),
]

def _shannon(s: str) -> float:
    if not s:
        return 0.0
    counts = Counter(s)
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())

def redact(text: str, *, entropy_threshold: float = 3.5, min_token_len: int = 20):
    meta = Counter()                                   # type -> count, NEVER values

    # Layer 1 — known formats (precise, labeled by type).
    for typ, pat in _KNOWN:
        if typ == "pg_conn_password":
            text, n = pat.subn(r"\g<pre>[REDACTED:pg_password]\g<post>", text)
        else:
            text, n = pat.subn(f"[REDACTED:{typ}]", text)
        if n:
            meta[typ] += n

    # Layer 2 — Shannon entropy for unknown high-entropy tokens.
    def _maybe_secret(m: re.Match) -> str:
        tok = m.group(0)
        if len(tok) >= min_token_len and _shannon(tok) >= entropy_threshold:
            meta["high_entropy"] += 1
            return "[REDACTED:high_entropy]"
        return tok
    text = re.sub(r"[A-Za-z0-9+/=_\-]{16,}", _maybe_secret, text)

    # Layer 3 — small PII classifier on the residual (sketch; model owned by doc 06 pipeline).
    text, pii_counts = _pii_classifier_redact(text)    # emails/names/phones/IPs → [REDACTED:pii.<kind>]
    meta.update(pii_counts)

    return text, dict(meta)
```

A concrete input → output:

**Input (`title` + raw body the Sentry connector rendered):**
```
DB connection failed for user checkout. DSN=postgres://app:S3cr3tP@ss@db.prod:5432/main
AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.sig
Contact ops: jane.doe@acme.io (+1-415-555-0142). Session sk_live_4eC39HqLyjWDarjtT1zdp7dc
```

**`body_redacted` (what is persisted):**
```
DB connection failed for user checkout. DSN=postgres://app:[REDACTED:pg_password]@db.prod:5432/main
AWS_ACCESS_KEY_ID=[REDACTED:aws_access_key_id] token=[REDACTED:jwt]
Contact ops: [REDACTED:pii.email] ([REDACTED:pii.phone]). Session [REDACTED:stripe_secret_key]
```

**`redaction_meta` (persisted on the `CanonicalEvent`; safe to store + show):**
```json
{
  "pg_password": 1,
  "aws_access_key_id": 1,
  "jwt": 1,
  "stripe_secret_key": 1,
  "pii.email": 1,
  "pii.phone": 1,
  "schema_version": "redactor.v3"
}
```

Note what *survived*: the structural, non-secret signal a specialist actually needs — that a Postgres DSN to `db.prod:5432/main` failed for service `checkout` — is fully intact. Redaction strips the secret, not the diagnostic meaning.

### 4.4 The precision/recall trade-off (we accept over-redaction)

The detectors are tuned for **recall over precision** — we would rather redact a harmless build hash than leak one API key (C2 dominates). The cost is real: an over-redacted token is signal a specialist loses (a commit SHA the Change/Deploy specialist wanted). We manage it three ways: (1) the regex layer's precise labels mean *most* redactions are typed, not blunt; (2) the entropy threshold is tuned against a labeled corpus in the eval pipeline (doc 06) to drop false positives without dropping recall below target; (3) common false positives (git SHAs, ULIDs, content hashes) get *allow-list* shapes so they survive when context confirms they're identifiers. The asymmetry is deliberate and permanent — a leaked secret is a breach; an over-redacted hash is a slightly-harder diagnosis. This is ADR-004's "recall cost accepted and tuned," made operational.

---

## 5. The immutable per-tenant audit log (hash-chain mechanism)

Doc 02 owns the `audit_log` *table* (columns `seq`, `tenant_id`, `entry_hash bytea`, `prev_hash bytea`, `actor`, `action`, `target`, `detail`, `at`; genesis `prev_hash` = 32 zero bytes). **This doc owns the mechanism that makes it tamper-evident and tenant-exportable.**

### 5.1 The chain

Each entry's `entry_hash` is `H(seq ‖ prev_hash ‖ canonical(row))` — `H` = SHA-256, `‖` = length-prefixed concatenation, `prev_hash` = the previous entry's `entry_hash` for the same tenant (genesis = 32 zero bytes). This makes the per-tenant log a **hash chain**: altering or deleting any historical entry changes its `entry_hash`, breaking every later link, and re-computing to hide it requires rewriting *every* subsequent row — detectable, and (with doc 02 §3.3's append-only grants giving the app role no `DELETE`/`UPDATE` on `audit_log`) something the application role cannot do at all.

`canonical(row)` deterministically serializes the semantic fields — `tenant_id, seq, actor, action, target, detail, at` — with sorted keys, fixed encoding, UTC timestamps, so the hash is reproducible byte-for-byte by anyone holding the row. That is what makes the chain *independently verifiable* and *exportable*. It is **per-tenant**, which is why doc 02 §1.2 can copy a tenant's chain on graduation and have it re-verify end-to-end with no re-hashing.

```python
# security/audit_chain.py — Python 3.12. Append + verify over doc 02's audit_log table.
import hashlib, json
from datetime import datetime

GENESIS = b"\x00" * 32

def _lp(b: bytes) -> bytes:                       # length-prefixed framing (no ambiguity)
    return len(b).to_bytes(8, "big") + b

def canonical(row: dict) -> bytes:
    """Deterministic serialization of the SEMANTIC fields. Sorted keys, UTC isoformat."""
    payload = {
        "tenant_id": row["tenant_id"], "seq": row["seq"], "actor": row["actor"],
        "action": row["action"], "target": row.get("target"),
        "detail": row["detail"],                  # already redacted (no secrets enter the log)
        "at": row["at"].astimezone().isoformat() if isinstance(row["at"], datetime) else row["at"],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str).encode("utf-8")

def entry_hash(seq: int, prev_hash: bytes, row: dict) -> bytes:
    h = hashlib.sha256()
    h.update(_lp(seq.to_bytes(8, "big")))
    h.update(_lp(prev_hash))
    h.update(_lp(canonical(row)))
    return h.digest()                              # 32 bytes → audit_log.entry_hash

class AuditChain:
    """Appends are serialized per-tenant by the UNIQUE(tenant_id, seq) constraint (doc 02):
    two concurrent appends race on the next seq and one loses + retries — gap-free, like
    incident_events (doc 02 §4.2). RLS scopes every read/write to the tenant."""

    async def append(self, session, *, tenant_id, actor, action, target=None, detail=None):
        async with session.begin():                # one tenant scope (doc 02 tenant_session)
            prev = (await session.execute(_SQL_LAST_HASH, {"t": tenant_id})).first()
            prev_hash, seq = (prev[0], prev[1] + 1) if prev else (GENESIS, 1)
            row = {"tenant_id": tenant_id, "seq": seq, "actor": actor, "action": action,
                   "target": target, "detail": detail or {}, "at": datetime.utcnow()}
            row_hash = entry_hash(seq, prev_hash, row)
            await session.execute(_SQL_INSERT, {**row, "entry_hash": row_hash, "prev_hash": prev_hash})
            return seq, row_hash

    async def verify(self, session, tenant_id) -> "VerifyResult":
        """Walk the chain in seq order; recompute each hash; assert linkage. O(n), exportable."""
        rows = await session.execute(_SQL_ALL_ORDERED, {"t": tenant_id})
        prev_hash = GENESIS
        for r in rows:
            expected = entry_hash(r.seq, prev_hash, r._mapping)
            if bytes(r.entry_hash) != expected:
                return VerifyResult(ok=False, broken_at=r.seq, reason="entry_hash mismatch")
            if bytes(r.prev_hash) != prev_hash:
                return VerifyResult(ok=False, broken_at=r.seq, reason="broken prev_hash linkage")
            prev_hash = bytes(r.entry_hash)
        return VerifyResult(ok=True, head=prev_hash)   # head doubles as a checkpointable anchor
```

### 5.2 What gets logged, and what never does

Every security- and governance-relevant act appends one entry: a JIT credential minted (§2.2), an `AutonomyGrant` created/revoked, an `ActionRequest` executed, a tenant data export, an integration connected/disconnected. The `detail` is **redacted** (doc 02's column) — `lease_id`, `scope`, `action_type`, never a credential or secret. The audit log records *authority spent*, not *data seen*.

### 5.3 Tenant-exportable as a trust & SOC2 feature

Because `entry_hash` is reproducible from the row alone, a tenant can **export their chain and verify it independently** — re-run `verify()` (or any SHA-256) over the exported rows and confirm the head hash. This is a product feature, not just compliance plumbing: it lets a security-conscious founder *audit Sentinel auditing itself* ("show me every time you touched my prod, and prove the list wasn't edited"). For SOC2 it is direct evidence for the audit-logging and integrity families (§7). The head hash can also be periodically **anchored** (published to an append-only external store) so even a control-plane operator with database write access cannot rewrite history without externally-provable divergence (§7 roadmap).

---

## 6. RBAC, dangerous-action tagging & tiered approval

Authority to *act* on a tenant's prod is governed by the autonomy ladder (doc 00 §6, doc 04 §7) — this doc does not redefine it; it secures the inputs the ladder trusts.

**RBAC (control-plane).** Human access is least-privilege and role-scoped: tenant dashboard users see only their own `tenant_id` (RLS, doc 02 — the API sets `app.tenant_id` from the authenticated session, never a request parameter); Sentinel operators have *no* standing read access to tenant `body_redacted`/memory (P10 — even redacted traces go through audited, time-boxed break-glass, itself chained); the router's AppRole (§3.2) is the only identity that can mint tenant creds and cannot read them. The principle: **no human and no LLM agent holds standing authority over a tenant's prod; only the tenant's grants + the router's per-call mint do.**

**Dangerous-action tagging.** An `action_type` is not trusted to self-describe at runtime; its danger is **declared as data** in two independent places the policy reads: the connector's `WriteCapability(action_type, reversible, requires_agent, min_tier_hint, idempotent)` (doc 03 §1.1, per provider — `db.failover` sets `requires_agent=True`, `deploy.rollback` is inherently reversible), and the Planner's `RemediationPlan.reversible` + `estimated_blast_radius` per incident (doc 04 §8).

The escalation engine (doc 04 §7) consumes both, and its two **hard stops fire first** — *irreversible → always page*, *LOW confidence → always page* — before any tier check, so no granted tier and no LLM judgment can authorize an autonomous irreversible write. This doc's contribution is the **independent reversibility floor**: since doc 04 §10's open risk is "the policy trusts the Planner's `reversible` absolutely," the router additionally consults a **rule-based reversibility classifier keyed on `action_type`** that can only ever *downgrade* `reversible`, never upgrade it. Two independent assessments must agree an action is reversible before it runs autonomously (P2 applied to the autonomy decision); `db.failover` or `config.set_env` against a shared resource is hard-pinned irreversible regardless of what any LLM claims.

**Tiered approval** is then exactly the §00.6 matrix: `T0` observe, `T1` suggest, `T2` one-tap approval, `T3` autonomous for `reversible + HIGH` only — every other path pages with the drafted fix. The chain (§5) records the tier in force and the rationale for every action.

---

## 7. STRIDE threat model

STRIDE over Sentinel's major assets and flows — each cell pairs the realistic **vector** with the **mitigation** (and the mechanism that carries it).

| Asset / flow | **S**poofing | **T**ampering | **R**epudiation | **I**nfo disclosure | **D**enial of service | **E**levation of privilege |
|---|---|---|---|---|---|---|
| **Tenant credentials (Vault)** | Forged router identity minting creds → AppRole + templated policy bind `tenant_id` (§3.2); deny-wildcard | Secret store altered → envelope encryption; transit KEK in KMS, ciphertext inert without it (§3.1) | "Sentinel used my key & denies it" → every mint hash-chained to `audit_log` (§5), tenant-exportable | **Worker memory dump leaks key** → no durable key in workers (P9); JIT TTL ≤ 60s; cred never logged (§2.2) | Vault unreachable → router degrades to **T0 + page** (fail-safe, C1); no auto-action without a mint | Confused deputy mints tenant-A's cred for tenant-B → graph closure binds `tenant_id` + Vault templated policy (§2) |
| **Canonical events / redacted store** | Forged webhook injects fake event → per-`integration_id` HMAC verify at edge (doc 03 §3) | Persisted event altered → append-only `events`, no `UPDATE`/`DELETE` grant (doc 02 §3.3) | — | **Secret persisted in `body_redacted`** → layered edge redaction *before* Kafka (§4); stores secret-free by construction | Alert-storm floods ingestion → per-tenant fair-share + rate limits (ADR-007); inbox dedup (doc 03 §3) | Cross-tenant read via forged filter → Postgres RLS ANDs tenant predicate (doc 02 §6); `sentinel_app` NOBYPASSRLS |
| **Agent mTLS tunnel** | Impersonate the edge to an agent → agent pins edge CA (doc 03 §7); TLS 1.3 mutual auth | Inject a forged `ActionRequest` down the tunnel → action signed/brokered via router; agent enforces local allow-list (doc 03 §7) | Agent ran an action it disowns → `action.audit` + chain entry per execution (§5, doc 02) | Telemetry sniffed in tenant net → first-pass redaction in-agent (P3); mTLS encrypts the wire | Tunnel flooded / reconnect storm → keepalive + jittered backoff; `max_inflight_actions` cap (doc 03 §7) | Stolen agent cert → short-TTL PKI leaf, rotated pre-expiry (§3.3); cert grants *transport*, not prod authority |
| **MCP Router (the spine)** | Caller spoofs a tenant → `tenant_id` from server-side closure, not args (§2.1) | Tamper with scope to widen authority → scope computed server-side from `(action,target)`; caller can't request broader (§2.2) | Router denies a write it brokered → mint + execute + revoke all hash-chained (§5) | Credential leaks via logs/trace → injected only into call headers; logger redaction; never in `args`/`detail` (§2.2) | **Router down → no tenant can auto-remediate** (correlated blast radius) → active-active HA across AZs; degrade to T0+page (§10, doc 01) | Over-broad lease → least-priv scope + TTL clamp; Vault deny-wildcard backstop (§3.2) |
| **Dashboard / API** | Session hijack / token replay → short-lived session tokens; tenant scope from session, never request param (§6) | Forged approval to trigger a write → one-tap approval bound to a signed `one_tap_action_id` resuming the exact HITL interrupt (doc 04 §9) | User denies approving an action → approval recorded as `human:<id>` in `incident_events` + chain (§5, doc 02) | One tenant reads another's incidents → API sets `app.tenant_id` from auth; RLS guarantee (doc 02 §6) | Auth endpoint flooded → global front-door rate limiting; tenant data planes are regional & isolated (doc 01 §9) | Tenant user escalates to operator → RBAC; operator tenant-data access is break-glass + logged (§6) |

The highest-severity row is **Router DoS** — the one place a per-tenant-isolation system has a *correlated* failure across all tenants (doc 01 §10). The mitigation is twofold: active-active across AZs with no shared mutable state (it mints fresh per call, holds nothing), and if Vault is unreachable the router **fails safe to T0 + page** — never falling back to a cached or long-lived credential, because that trades C2/P9 for availability. We choose "page a human" over "lower the security bar," every time.

---

## 8. SOC2 control mapping & roadmap to Type II

Sentinel front-loads the controls a SOC2 Type II audit examines; most are *mechanisms C1/C2 already require*, not bolt-ons. Mapping the Trust Services Criteria (TSC) to the implementing mechanism:

| TSC family | Control objective | Implementing mechanism (this design) | Evidence source |
|---|---|---|---|
| **CC6.1 Logical access** | Least-privilege access to data & systems | RLS with `sentinel_app` NOBYPASSRLS (doc 02 §3); per-call JIT creds, no standing keys (P9, §3) | RLS policies in IaC; Vault policies (§3.2); the doc 02 §6 cross-tenant test as a merge gate |
| **CC6.1 / CC6.6 Boundary protection** | Restrict & authenticate ingress | Outbound-only (P4, ADR-002); mTLS agent + HMAC webhooks (doc 01 §3, doc 03 §3) | mTLS/PKI config; edge auth logs |
| **CC6.3 Credential management** | Provision, rotate, revoke credentials | Vault envelope encryption; TTL ≤ 60s JIT; KEK/PKI rotation (§3) | Vault lease & rotation logs; the `audit_log` mint entries (§5) |
| **CC6.7 Data-in-transit / at-rest** | Encrypt sensitive data | TLS 1.3 everywhere; envelope-encrypted secrets; **redaction so secrets are never at rest** (§4) | TLS config; `redaction_meta` corpus; backup-scan showing no secrets |
| **CC7.2 Monitoring / anomaly detection** | Detect security events | Per-tenant rate limits + circuit breaker (ADR-010); unhealthy-integration alerts (doc 03 §4); Langfuse traces (doc 06) | Alert config; breaker trip logs |
| **CC7.3 / CC8.1 Change management** | Authorize & track changes | GitOps (ArgoCD), eval-gated CI (doc 06); IaC for all policy | Git history; ArgoCD sync logs; CI gate results |
| **CC4.1 / A1.2 Audit logging & integrity** | Tamper-evident activity record | **Hash-chained per-tenant `audit_log` (§5); tenant-exportable + verifiable** | The chain itself; `verify()` output; external anchor (roadmap) |
| **C1.1 / P-series Confidentiality & privacy** | Limit data collection & use | C2/P3 redaction; P10 evals on redacted traces only (ADR-012); collection minimized by scope (doc 03 §4) | Redaction config; eval pipeline (doc 06); data-flow diagram (§1) |
| **A1.1 Availability** | Resilience & recovery | Stateless workers + durable checkpoints (ADR-006); active-active router; KEDA autoscale (doc 01 §7) | Uptime SLO; chaos-test results; HA topology |
| **PI1 Processing integrity** | Correct, authorized processing | Calibrated-confidence gating (P6); pure escalation policy (doc 04 §7); idempotent metering (ADR-010) | Policy unit tests (doc 06); calibration curves |

**Roadmap to Type II.** Type I attests the controls are *designed* correctly at a point in time; **Type II attests they *operated* effectively over a 6–12-month window** — an evidence-generation problem the audit chain is purpose-built for. The path: (1) formalize control narratives against the table and freeze policy in IaC (the controls already exist); (2) wire **continuous evidence** — the hash-chained `audit_log`, ArgoCD sync history, CI eval-gate results, and Vault lease logs *are* the operating-effectiveness evidence, collected automatically rather than screenshot at audit time; (3) add the **external anchor** for the chain head (§5.3) so integrity is provable without trusting our database; (4) run a Type I, then accumulate the window into a Type II. The strategic point: because the controls are mechanisms the product *needs*, the "operating effectiveness" evidence is a byproduct of normal operation — the chain that proves to a *tenant* we touched their prod honestly is the same one that proves it to an *auditor*.

---

## 9. Trust posture as marketing: safer than SSHing into prod half-asleep

The hardest objection a founder raises is emotional: *"I'm not letting a robot touch my production."* The honest, literal answer is that **the alternative — you, alone, paged at 3 a.m., SSHing into prod half-asleep — is the less safe option**, and every mechanism here is why that's true rather than glib:

- **You have ambient root; Sentinel has none.** Your SSH key is standing, long-lived authority over all of prod, on a device that goes to coffee shops. Sentinel holds *no* standing tenant credential (P9); it mints one scoped to *one action*, valid for *seconds*, and cannot touch a resource outside the incident's scope (§2). Compromising *you* exposes your whole infrastructure; compromising a Sentinel worker exposes one tenant's 60-second token.
- **You act on a hunch; Sentinel acts on calibrated evidence — or refuses.** Half-asleep, you'll run the command you *think* fixes it. Sentinel executes nothing unless confidence is *calibrated* HIGH, the action is *reversible*, and the tenant *granted* the tier (doc 04 §7); otherwise it pages you with the diagnosis done. Irreversible actions *always* page, at any trust level (C1) — constitutionally more cautious than a tired human.
- **You might fat-finger an irreversible command; Sentinel cannot.** The two hard stops (irreversible, LOW confidence) fire before any autonomy check and *cannot be overridden* by any grant, and the independent reversibility floor (§6) requires two assessments to agree before anything runs unattended.
- **Your 3 a.m. session leaves no reliable record; Sentinel's is tamper-evident.** It hash-chains every action and every credential spent (§5) and hands you a log you can *verify wasn't edited* — even by us.
- **You *see everything*; Sentinel sees almost nothing.** Your eyes are on raw logs full of secrets; Sentinel reasons over text that had every secret stripped at the edge before storage (§4) — it cannot leak what it never held.

This is the marketing line because it is the engineering reality: **least privilege, calibrated action, reversibility-gating, tamper-evident audit, and minimized visibility are not features bolted onto a risky product — they are the product, each strictly safer than the human-with-root status quo.** For a tool touching prod, that *is* the pitch.

---

## Open questions & risks

- **Redaction recall is the whole confidentiality guarantee — and it is probabilistic at layer 3.** Regex (layer 1) is deterministic, but the entropy threshold and the PII classifier (layers 2–3) have a measurable miss rate. A novel secret format with low entropy (a short, dictionary-word API key) could slip both nets. We accept over-redaction (§4.4), but the residual false-negative rate needs a continuously-evaluated bound (doc 06) and a documented incident-response path for a confirmed leak — not yet specified.
- **Router availability vs. the no-cached-credential rule.** Failing safe to T0 + page when Vault is unreachable (§7) is correct for security but means a Vault outage degrades *every* tenant to manual at once — a correlated blast radius that contradicts our per-tenant isolation story (doc 01 §10). The active-active + Vault-HA design is asserted; the precise RTO and the degradation UX under a regional Vault partition are not yet load-tested.
- **External anchoring of the audit head is roadmap, not built.** Until the chain head is published to an append-only external store (§5.3), a control-plane operator with direct database write access *could* rewrite a tenant's history and recompute the chain. The hash-chain makes this detectable only by someone holding a prior head; the anchor closes the gap and is required for the strongest SOC2 integrity claim.
- **Local (agent) redaction parity with the edge.** Doc 03 §7's agent runs a "first pass" of redaction in a 64 MiB Go container, but layer 3 (the PII classifier) is a Python/ML stage here. Either the agent ships a distilled model or it does regex+entropy only and the edge does the ML pass on still-tenant-scoped data. The split is unowned across this doc and doc 03 and needs a joint decision.
- **Break-glass operator access is logged but not yet quorum-gated.** §6 routes operator access to tenant data through audited break-glass, but a single compromised operator account is still a single point of failure for confidentiality. Two-person / quorum approval for break-glass (and for KEK rotation) is implied by the posture but not specified.
- **Confused-deputy assurance is an argument, not yet a proof.** §2's claim that cross-tenant tool access is *unrepresentable* rests on the graph closure being truly immutable and the router never constructing a `BrokerRequest` from LLM-influenced data. This deserves a property-based test (fuzz tool-call args with foreign `tenant_id`s and assert the minted path is always the graph's own) and ideally a typed wrapper that makes a cross-tenant `BrokerRequest` fail to compile — currently enforced by convention and review.
