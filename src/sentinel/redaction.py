"""Edge-side redaction pipeline (doc 05 §4, ADR-004, P3).

Runs at the ingestion edge in the ``authenticate → redact → normalize → produce``
order (doc 01 §3), filling the ``body_redacted`` placeholder a connector's
``normalize()`` leaves empty (doc 03 §1.1) *before* anything is written to Kafka.
Redacting here — not at rest — is what makes the durable stores (Postgres,
Qdrant, object storage, backups) secret-free *by construction* (doc 05 §4.1).

Three detector layers run **in series, cheapest-and-most-precise first**, each
layer's output feeding the next (doc 05 §4.2):

1. **Known formats (regex, high precision).** Unambiguous token shapes are
   labelled by *type* — so a known secret yields a precise ``redaction_meta``
   type rather than a generic ``high_entropy`` (doc 05 §4.2).
2. **Shannon entropy (the net for the unknown).** Token-shaped substrings whose
   length and per-character entropy clear the thresholds are treated as probable
   secrets even if they match no known format. It runs *after* layer 1 so
   already-labelled tokens are not re-flagged or double-counted (doc 05 §4.2).
3. **PII (regex stand-in).** Emails, phones, and IPv4 on the residual.

   .. note::
      Layer 3 is a deterministic regex **stand-in** for the distilled NER-style
      PII classifier owned by the doc 06 evaluation pipeline (doc 05 §4.2). The
      shapes here are intentionally broad; the production layer is an ML model.

Detectors are tuned for **recall over precision** — over-redacting a harmless
build hash is acceptable; leaking one API key is not (doc 05 §4.4, C2). The
emitted :class:`RedactionResult.meta` is types→counts only and *never* values
(doc 05 §4.3), so it is itself safe to persist and to surface on the trust
dashboard.
"""

from __future__ import annotations

import math
import re
from collections import Counter

from pydantic import BaseModel, ConfigDict, Field

# --- Layer 1: known token formats, ordered highest-precision first (doc 05 §4.3). --
# Each entry is (redaction_meta type, compiled pattern). The Postgres DSN entry is
# handled specially below to preserve the surrounding URL structure.
_KNOWN: list[tuple[str, re.Pattern[str]]] = [
    ("aws_access_key_id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("github_pat", re.compile(r"\bghp_[A-Za-z0-9]{36}\b")),
    ("stripe_secret_key", re.compile(r"\b[rs]k_live_[A-Za-z0-9]{24,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")),
    (
        "pem_private_key",
        re.compile(
            r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----[\s\S]+?-----END[^-]+-----"
        ),
    ),
]

# Postgres DSN password: replace ONLY the password, preserving ``user`` and the
# ``@host:port/db`` tail (doc 05 §4.3). The password may itself contain '@'
# (e.g. ``S3cr3tP@ss``); the match runs up to the '@' that precedes the host,
# identified by lookahead, so structure survives intact and the meta type is
# the doc's ``pg_password`` (not ``pg_conn_password``).
_PG_CONN_PASSWORD: re.Pattern[str] = re.compile(
    r"(?P<pre>postgres(?:ql)?://[^:@/\s]+:)"  # scheme + user + ':'
    r"[^@\s]+(?:@[^@\s]+)*?"  # password (non-greedy; may embed '@')
    r"(?P<post>@(?=[^@/\s:]+(?::\d+)?(?:[/?]|$)))"  # '@' before host[:port][/?]
)

# --- Layer 2: token shape scanned for Shannon entropy (doc 05 §4.3). ---
_TOKEN_SHAPE: re.Pattern[str] = re.compile(r"[A-Za-z0-9+/=_-]{16,}")

# --- Layer 3: PII stand-in for the doc 06 ML classifier (doc 05 §4.2). ---
_PII: list[tuple[str, re.Pattern[str]]] = [
    ("pii.email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("pii.phone", re.compile(r"(?<!\w)\+?\d[\d\s().-]{7,}\d(?!\w)")),
    (
        "pii.ip",
        re.compile(
            r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"
        ),
    ),
]

SCHEMA_VERSION = "redactor.v3"


class RedactionResult(BaseModel):
    """The output of :func:`redact`: the redacted body plus safe-to-store metadata.

    Immutable (``frozen``) and closed to unknown fields (``extra="forbid"``), per
    the core's value-object convention. ``meta`` maps each detection *type* to a
    count and **never** carries a redacted value (doc 05 §4.3).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    meta: dict[str, int] = Field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION


def _shannon_entropy(token: str) -> float:
    """Shannon entropy (bits/symbol) over a token's character distribution."""
    if not token:
        return 0.0
    n = len(token)
    counts = Counter(token)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def redact(
    text: str, *, entropy_threshold: float = 3.5, min_token_len: int = 20
) -> RedactionResult:
    """Redact secrets and PII from ``text`` via the three layered detectors.

    Layers run in order (regex → entropy → PII), each consuming the previous
    layer's output, so a known secret is labelled by type before entropy ever
    sees it (doc 05 §4.2). Tuned for recall over precision (doc 05 §4.4).

    Args:
        text: The raw body to redact (may be empty).
        entropy_threshold: Minimum Shannon entropy (bits/symbol) for a
            token-shaped substring to count as high-entropy in layer 2.
        min_token_len: Minimum length for layer-2 consideration; shorter tokens
            (e.g. a 7-char git SHA) are spared as probable identifiers.

    Returns:
        A :class:`RedactionResult` with the redacted ``text`` and a
        types→counts ``meta`` (never values).
    """
    meta: Counter[str] = Counter()

    # Layer 1 — known formats (precise, labelled by type). DSN password first so
    # its embedded password cannot be misread as a high-entropy token in layer 2.
    text, n = _PG_CONN_PASSWORD.subn(r"\g<pre>[REDACTED:pg_password]\g<post>", text)
    if n:
        meta["pg_password"] += n
    for typ, pattern in _KNOWN:
        text, n = pattern.subn(f"[REDACTED:{typ}]", text)
        if n:
            meta[typ] += n

    # Layer 2 — Shannon entropy for the long tail of unknown secrets. Runs after
    # layer 1, so the inserted ``[REDACTED:...]`` placeholders (which carry no
    # high-entropy run) are not re-flagged (doc 05 §4.2).
    def _maybe_secret(match: re.Match[str]) -> str:
        token = match.group(0)
        if len(token) >= min_token_len and _shannon_entropy(token) >= entropy_threshold:
            meta["high_entropy"] += 1
            return "[REDACTED:high_entropy]"
        return token

    text = _TOKEN_SHAPE.sub(_maybe_secret, text)

    # Layer 3 — PII stand-in on the residual (doc 06's ML classifier in prod).
    for typ, pattern in _PII:
        text, n = pattern.subn(f"[REDACTED:{typ}]", text)
        if n:
            meta[typ] += n

    return RedactionResult(text=text, meta=dict(meta))
