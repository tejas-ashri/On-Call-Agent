"""Tests for the edge-side redaction pipeline (doc 05 §4).

The load-bearing test is :func:`test_doc_worked_example_verbatim`, which pins the
exact input → output contract from doc 05 §4.3 (``body_redacted`` +
``redaction_meta``). The remaining tests cover the empty/clean baselines, the
layer-2 entropy net for unknown secrets, and the recall/precision floor that a
short git SHA must survive (below ``min_token_len``).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sentinel.redaction import RedactionResult, redact

# Doc 05 §4.3 worked example, transcribed verbatim. ``S3cr3tP@ss`` deliberately
# embeds an '@' inside the Postgres password to exercise structure preservation.
_DOC_INPUT = (
    "DB connection failed for user checkout. "
    "DSN=postgres://app:S3cr3tP@ss@db.prod:5432/main\n"
    "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE "
    "token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.sig\n"
    "Contact ops: jane.doe@acme.io (+1-415-555-0142). "
    "Session sk_live_4eC39HqLyjWDarjtT1zdp7dc"
)

_DOC_BODY_REDACTED = (
    "DB connection failed for user checkout. "
    "DSN=postgres://app:[REDACTED:pg_password]@db.prod:5432/main\n"
    "AWS_ACCESS_KEY_ID=[REDACTED:aws_access_key_id] token=[REDACTED:jwt]\n"
    "Contact ops: [REDACTED:pii.email] ([REDACTED:pii.phone]). "
    "Session [REDACTED:stripe_secret_key]"
)

_DOC_REDACTION_META = {
    "pg_password": 1,
    "aws_access_key_id": 1,
    "jwt": 1,
    "stripe_secret_key": 1,
    "pii.email": 1,
    "pii.phone": 1,
}


def test_doc_worked_example_verbatim() -> None:
    """The doc 05 §4.3 example: text and meta must match the spec byte-for-byte."""
    result = redact(_DOC_INPUT)
    assert result.text == _DOC_BODY_REDACTED
    assert result.meta == _DOC_REDACTION_META
    # Worked example has no high-entropy hit: regex labels each secret precisely.
    assert "high_entropy" not in result.meta


def test_schema_version_is_redactor_v3() -> None:
    assert redact("").schema_version == "redactor.v3"
    assert RedactionResult(text="x", meta={}).schema_version == "redactor.v3"


def test_result_is_frozen_value_object() -> None:
    result = redact("nothing to see here")
    # Frozen pydantic value object: reassignment must raise.
    with pytest.raises(ValidationError):
        result.text = "mutated"  # type: ignore[misc]


def test_empty_string_yields_empty_text_and_no_meta() -> None:
    result = redact("")
    assert result.text == ""
    assert result.meta == {}


def test_clean_text_is_unchanged_with_no_meta() -> None:
    clean = "deploy svc-checkout to prod region us-east-1 at 14:03 utc, all green"
    result = redact(clean)
    assert result.text == clean
    assert result.meta == {}


def test_unknown_high_entropy_token_is_caught_by_layer_two() -> None:
    """A bespoke 40-char secret matches no known format → entropy net (layer 2)."""
    secret = "Zx9kQ2pLm7vTn4wRy8bGcH3sJ6dFa0eUiOoPqW1Z"
    result = redact(f"session_token={secret} continue")
    assert "[REDACTED:high_entropy]" in result.text
    assert secret not in result.text
    assert result.meta == {"high_entropy": 1}


def test_short_git_sha_is_not_redacted() -> None:
    """A 7-char SHA is below ``min_token_len`` (20): a survivable identifier."""
    text = "fix landed in commit a1b2c3d on main"
    result = redact(text)
    assert result.text == text
    assert "a1b2c3d" in result.text
    assert result.meta == {}


def test_min_token_len_floor_blocks_otherwise_high_entropy_short_token() -> None:
    """A 16-char high-entropy token is matched by the scanner but spared by length."""
    short_token = "Zx9kQ2pLm7vTn4wR"  # 16 chars: matches scanner regex, < min_token_len
    assert len(short_token) == 16
    result = redact(f"id={short_token} done")
    assert result.text == f"id={short_token} done"
    assert result.meta == {}


def test_pii_email_phone_ip_are_labelled_and_counted() -> None:
    text = "from 10.0.0.5 user a@b.io called +1-202-555-0188 today"
    result = redact(text)
    assert "[REDACTED:pii.ip]" in result.text
    assert "[REDACTED:pii.email]" in result.text
    assert "[REDACTED:pii.phone]" in result.text
    assert result.meta == {"pii.ip": 1, "pii.email": 1, "pii.phone": 1}


def test_meta_never_carries_redacted_values() -> None:
    """``meta`` is type→count only; no secret value leaks into the keys or values."""
    result = redact(_DOC_INPUT)
    assert all(isinstance(v, int) for v in result.meta.values())
    assert "S3cr3tP" not in str(result.meta)
    assert "AKIAIOSFODNN7EXAMPLE" not in str(result.meta)
    assert "sk_live_4eC39HqLyjWDarjtT1zdp7dc" not in str(result.meta)


def test_github_pat_and_pem_block_are_redacted() -> None:
    pat = "ghp_" + "a1B2c3D4e5" * 3 + "abcdef"  # ghp_ + 36 chars
    assert len(pat) == 40
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIBOgIBAAJBAKj34GkxFh\n"
        "-----END RSA PRIVATE KEY-----"
    )
    result = redact(f"key={pat}\n{pem}")
    assert "[REDACTED:github_pat]" in result.text
    assert "[REDACTED:pem_private_key]" in result.text
    assert result.meta["github_pat"] == 1
    assert result.meta["pem_private_key"] == 1
    assert "BEGIN RSA PRIVATE KEY" not in result.text
