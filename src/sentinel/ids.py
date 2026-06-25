"""Prefixed, k-sortable ULID identifiers (doc 00 §4).

Every entity carries a human-readable prefix (`inc_`, `evt_`, …) on a 26-char
Crockford-base32 ULID: a 48-bit millisecond timestamp followed by 80 bits of
randomness. The timestamp leads, so lexicographic ordering follows creation
time (k-sortability). Time and randomness can be injected for deterministic
tests. No external dependencies.
"""

from __future__ import annotations

import os
import time

# Crockford base32 alphabet (excludes I, L, O, U to avoid ambiguity).
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CROCKFORD_SET = frozenset(_CROCKFORD)

_TIME_LEN = 10  # 10 * 5 bits = 50 bits, holds the 48-bit ms timestamp
_RAND_LEN = 16  # 16 * 5 bits = 80 bits of randomness
ULID_LEN = _TIME_LEN + _RAND_LEN  # 26

# Entity kind -> identifier prefix (doc 00 §4).
PREFIXES: dict[str, str] = {
    "tenant": "ten",
    "service": "svc",
    "event": "evt",
    "incident": "inc",
    "action": "act",
    "integration": "int",
    "grant": "grant",
}


def _encode(value: int, length: int) -> str:
    """Encode a non-negative int as a fixed-width Crockford-base32 string."""
    chars = []
    for _ in range(length):
        value, rem = divmod(value, 32)
        chars.append(_CROCKFORD[rem])
    return "".join(reversed(chars))


def generate_ulid(*, _ms: int | None = None, _rand: bytes | None = None) -> str:
    """Return a 26-char ULID. Inject ``_ms``/``_rand`` for deterministic tests."""
    ms = _ms if _ms is not None else int(time.time() * 1000)
    rand = _rand if _rand is not None else os.urandom(10)
    return _encode(ms, _TIME_LEN) + _encode(int.from_bytes(rand, "big"), _RAND_LEN)


def new_id(kind: str, *, _ms: int | None = None, _rand: bytes | None = None) -> str:
    """``new_id("incident") -> "inc_01J..."``. ``kind`` must be a known entity."""
    try:
        prefix = PREFIXES[kind]
    except KeyError:
        raise ValueError(f"unknown id kind: {kind!r}") from None
    return f"{prefix}_{generate_ulid(_ms=_ms, _rand=_rand)}"


def is_valid_id(value: str, kind: str) -> bool:
    """True iff ``value`` is ``<prefix>_<26-char ULID>`` for the given ``kind``."""
    prefix = PREFIXES.get(kind)
    if prefix is None:
        return False
    head, sep, ulid = value.partition("_")
    if not sep or head != prefix or len(ulid) != ULID_LEN:
        return False
    return all(c in _CROCKFORD_SET for c in ulid)
