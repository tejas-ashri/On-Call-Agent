"""Webhook idempotency key derivation (doc 03 §3).

Providers retry: Sentry resends on any non-2xx; Vercel and GitHub redeliver on
timeout or 5xx. The edge dedups on a deterministic key so the same delivery is
never normalized twice (which could double-emit incidents/pages and, if a write
were ever triggered twice, violate C1). This module owns the load-bearing key
logic from doc 03 §3: prefer the provider's own delivery id, fall back to a
content hash of the semantically-identifying subset of the payload.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable

from sentinel.connectors.base import RawSignal

# Provider-supplied delivery ids we trust as globally unique per integration
# (doc 03 §3 / §2: "the cleanest dedup keys are provider-supplied").
_NATIVE_KEYS: dict[str, Callable[[RawSignal], str | None]] = {
    "github": lambda r: r.headers.get("x-github-delivery"),
    "vercel": lambda r: _as_str(r.payload.get("id")),  # webhook delivery id
    "grafana": lambda r: _as_str(r.payload.get("groupKey")),  # alert group is stable
}

# Per-connector projection of the fields that *identify* a delivery. Volatile
# fields (our ``received_at``, receipt timestamps) are excluded so a legitimate
# retry hashes identically instead of slipping through as "new" (doc 03 §3).
_IDENTITY_SUBSET: dict[str, tuple[str, ...]] = {
    # Sentry's event_id is globally unique; issue_id is the dedup root (§6.1).
    "sentry": ("event_id", "issue_id"),
}


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _stable_json(obj: object) -> str:
    """Deterministic JSON: sorted keys, no insignificant whitespace (doc 03 §3)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _canonical_identity_subset(payload: dict[str, object], provider: str) -> object:
    """Project the payload to its identifying subset (doc 03 §3 per-connector).

    For Sentry the identity lives under ``data.event``; the generic fallback
    hashes the whole payload (still excludes ``received_at``, which is never in
    the payload). Selecting a subset keeps the hash stable across retries even
    if the provider decorates the payload with volatile envelope fields.
    """
    keys = _IDENTITY_SUBSET.get(provider)
    if keys is None:
        return payload
    event = payload.get("data")
    inner = event.get("event") if isinstance(event, dict) else None
    source = inner if isinstance(inner, dict) else payload
    return {k: source.get(k) for k in keys if k in source}


def idempotency_key(raw: RawSignal, provider: str) -> str:
    """Deterministic dedup key for an inbound delivery (doc 03 §3).

    Native form ``"{provider}:{id}"`` when the provider supplies a trusted
    delivery id; otherwise ``"{provider}:h:" + sha256(stable-json subset)[:32]``.
    """
    native = _NATIVE_KEYS.get(provider)
    if native and (k := native(raw)):
        return f"{provider}:{k}"
    ident = _canonical_identity_subset(raw.payload, provider)
    digest = hashlib.sha256(_stable_json(ident).encode()).hexdigest()[:32]
    return f"{provider}:h:{digest}"
