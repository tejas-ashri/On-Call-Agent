# Core Contracts & Escalation Engine — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the foundational, infra-free core of Project Sentinel — the canonical domain contracts (doc 00 §5), the inter-agent contracts (doc 04 §4), and the pure graduated-escalation engine (doc 04 §7) — as fully-typed, fully-tested Python.

**Architecture:** A `src/`-layout Python package `sentinel` with three concerns: `ids`/`enums` (canonical vocabulary), `contracts/` (Pydantic v2 models — the data shapes everything exchanges), and `escalation` (the pure, deterministic policy function that *is* the product). No Kafka/Postgres/LLM/Vault yet — this slice runs and tests in isolation, exactly as the constitution's "design for isolation" demands, and is the dependency root for every later phase.

**Tech Stack:** Python 3.12+ (running on 3.14), Pydantic v2, pytest, ruff, mypy `--strict`. Per doc 00 §7.

**Scope note:** This is sub-project 1 of the Phase-A roadmap (doc 08 §6, Week 1's "canonical event schema" + the product-defining escalation logic). It produces working, tested software on its own. Connectors, ingestion edge, LangGraph orchestration, persistence/RLS, etc. are later plans.

---

## File structure

| File | Responsibility |
|---|---|
| `pyproject.toml` | Package metadata, deps, ruff/mypy/pytest config |
| `src/sentinel/__init__.py` | Package version |
| `src/sentinel/ids.py` | Prefixed, k-sortable ULID identifiers (doc 00 §4) |
| `src/sentinel/enums.py` | Canonical enums + the confidence-band cut (doc 00 §4–§6) |
| `src/sentinel/contracts/__init__.py` | Re-exports the contract models |
| `src/sentinel/contracts/events.py` | `CanonicalEvent` (doc 00 §5.1) |
| `src/sentinel/contracts/autonomy.py` | `AutonomyGrant` (doc 00 §5.2) |
| `src/sentinel/contracts/actions.py` | `ActionRequest`, `ActionResult` (doc 00 §5.4) |
| `src/sentinel/contracts/agents.py` | Inter-agent contracts (doc 04 §4) |
| `src/sentinel/escalation.py` | `PolicyInputs` + `decide_escalation` (doc 04 §7.2) — the star |
| `tests/test_ids.py` … `tests/test_escalation.py` | One test module per source module |

---

## Task 0: Project scaffold

**Files:** Create `pyproject.toml`, `src/sentinel/__init__.py`, `tests/__init__.py`.

- [ ] **Step 1:** Write `pyproject.toml` with `requires-python=">=3.12"`, dep `pydantic>=2,<3`, dev deps `pytest/ruff/mypy`, and tool config: `ruff` (line-length 100, lint select E/F/I/UP/B/SIM), `mypy` (`strict=true`, `mypy_path="src"`, `files=["src","tests"]`), `pytest` (`pythonpath=["src"]`, `testpaths=["tests"]`).
- [ ] **Step 2:** `src/sentinel/__init__.py` → `__version__ = "0.0.1"`.
- [ ] **Step 3:** Run `./.venv/bin/ruff check .` → expect "All checks passed" (no files yet beyond scaffold).
- [ ] **Step 4:** Commit (deferred — see "Version control" note).

## Task 1: Prefixed ULID identifiers (`ids.py`)

- [ ] **Step 1 — failing test** (`tests/test_ids.py`): assert `new_id("incident")` starts with `inc_`, the ULID part is 26 Crockford chars; injected `_ms`/`_rand` make it deterministic; `is_valid_id` accepts valid / rejects wrong-prefix / wrong-length; `new_id("bogus")` raises `ValueError`; larger `_ms` sorts lexicographically after smaller (k-sortability).
- [ ] **Step 2:** Run `./.venv/bin/pytest tests/test_ids.py` → FAIL (module missing).
- [ ] **Step 3 — implement** `ids.py`: `PREFIXES` map (tenant→ten, service→svc, event→evt, incident→inc, action→act, integration→int, grant→grant), Crockford base32 `_encode`, `generate_ulid(_ms,_rand)` (48-bit ms + 80-bit rand → 26 chars), `new_id(kind)`, `is_valid_id(value,kind)`.
- [ ] **Step 4:** Run pytest → PASS. `mypy --strict` clean.

## Task 2: Canonical enums (`enums.py`)

- [ ] **Step 1 — failing test** (`tests/test_enums.py`): enum string values match the constitution verbatim (`ActionType.DEPLOY_ROLLBACK == "deploy.rollback"`, tiers `"T0".."T3"`, etc.); **`band_for` boundary table**: `0.0,0.49→LOW`; `0.5,0.79→MEDIUM`; `0.8,1.0→HIGH`; `-0.1` and `1.1` raise `ValueError`.
- [ ] **Step 2:** pytest → FAIL.
- [ ] **Step 3 — implement** `enums.py`: `Severity`, `EventKind`, `IntegrationMethod`, `ActionType`, `AutonomyTier`, `ConfidenceBand`, `IncidentState`, `ActionStatus` (all `StrEnum`); constants `BAND_MEDIUM_THRESHOLD=0.5`, `BAND_HIGH_THRESHOLD=0.8`; `band_for(confidence)->ConfidenceBand` (raises outside [0,1]). These thresholds are the single source of truth doc 04/06 reference.
- [ ] **Step 4:** pytest → PASS; mypy clean.

## Task 3: Data contracts — events, autonomy, actions

- [ ] **Step 1 — failing test** (`tests/test_contracts.py`): construct a `CanonicalEvent` with all required fields; assert `frozen` (mutation → `ValidationError`) and `extra="forbid"` (unknown field → error); `AutonomyGrant(...).active` True when `revoked_at` None, False otherwise; `ActionRequest` accepts canonical `status`/tier values and rejects a bad `status`.
- [ ] **Step 2:** pytest → FAIL.
- [ ] **Step 3 — implement** `contracts/events.py` (`CanonicalEvent`, fields per doc 00 §5.1, `extra="forbid"`, `frozen=True`), `contracts/autonomy.py` (`AutonomyGrant` + `active` property), `contracts/actions.py` (`ActionRequest` per §5.4, `ActionResult`). Re-export all from `contracts/__init__.py`.
- [ ] **Step 4:** pytest → PASS; mypy clean.

## Task 4: Inter-agent contracts (`contracts/agents.py`)

- [ ] **Step 1 — failing test** (`tests/test_agents.py`): `SpecialistFinding` with `self_confidence` out of `[0,1]` → `ValidationError`; `ConfidenceAssessment` is `frozen`; `EscalationDecision.outcome` accepts only the five canonical literals; `PagePayload` carries `suspected_root_cause`, `evidence_chain`, `competing_hypotheses`, `drafted_remediation`.
- [ ] **Step 2:** pytest → FAIL.
- [ ] **Step 3 — implement** `contracts/agents.py` exactly per doc 04 §4: `EvidenceRef`, `SpecialistFinding`, `Hypothesis`, `ConfidenceAssessment`, `RemediationPlan`, `EscalationDecision`, `PagePayload`, the `SpecialistRole` literal, `Confidence` annotated float `[0,1]`, and an `EscalationOutcome` literal.
- [ ] **Step 4:** pytest → PASS; mypy clean.

## Task 5: The graduated-escalation engine (`escalation.py`) ★

- [ ] **Step 1 — failing test** (`tests/test_escalation.py`):
  - **Exhaustive cube** (doc 06 §3.1): for every `(band, tier, reversible)` in `product(ConfidenceBand, AutonomyTier, [True,False])`, `decide_escalation(...)` returns exactly one of the five outcomes, and `outcome=="act_and_report"` **iff** `reversible and band is HIGH and tier is T3`.
  - **Worked rows** (doc 04 §7.3): `(HIGH,T3,True)→act_and_report/REMEDIATING`; `(MEDIUM,T3,True)→page_one_tap/AWAITING_APPROVAL`; `(HIGH,T2,True)→page_one_tap`; `(HIGH,T0,True)→page_with_drafted_fix/ESCALATED`; `(any,any,False)→page_irreversible/ESCALATED`; `(LOW,T3,True)→page_low_confidence/ESCALATED`.
  - **Hard-stop ordering**: `(LOW, T3, False)` → `page_irreversible` (irreversible checked before LOW).
- [ ] **Step 2:** pytest → FAIL.
- [ ] **Step 3 — implement** `escalation.py`: frozen `PolicyInputs(incident_id, band, max_tier, reversible, n_competing)` and `decide_escalation(i)->EscalationDecision`, copied faithfully from doc 04 §7.2 — the two hard stops (irreversible, LOW) **first**, then the T0/T1 → page-with-fix, T2 → one-tap, T3 → HIGH-only act / MEDIUM falls back to one-tap.
- [ ] **Step 4:** pytest → PASS; mypy clean.

## Task 6: Full green gate

- [ ] **Step 1:** `./.venv/bin/ruff check .` → all pass.
- [ ] **Step 2:** `./.venv/bin/mypy --strict` (via config) → no errors.
- [ ] **Step 3:** `./.venv/bin/pytest` → all tests pass.
- [ ] **Step 4:** Commit the slice (deferred — see note).

---

## Version control note

The directory is not yet a git repo and the user has not asked to commit. Per the "commit only when asked" rule, execution will build and verify everything but **hold commits** until the user confirms; the commit steps above document intent. Offer `git init` + an initial commit at the end.

## Self-review

- **Spec coverage:** `CanonicalEvent` (00 §5.1) ✓ Task 3; `AutonomyGrant` (§5.2) ✓ Task 3; `ActionRequest` (§5.4) ✓ Task 3; lifecycle states (§5.3) ✓ `IncidentState` Task 2; tiers/bands (§6) ✓ Task 2; inter-agent contracts (04 §4) ✓ Task 4; escalation matrix (04 §7 / 06 §3.1) ✓ Task 5; identifiers (00 §4) ✓ Task 1.
- **Placeholders:** none — every task names exact files and concrete behavior; code is specified by doc reference + signatures.
- **Type consistency:** `ConfidenceBand`/`AutonomyTier` (enums.py) are imported by agents.py and escalation.py; `EscalationDecision` defined in agents.py and returned by escalation.py; `band_for` thresholds reused by later `score_confidence`. Names consistent across tasks.
