"""Runtime configuration — where the Claude key is read from (doc 05, doc 07).

The key lives in a gitignored ``.env`` at the repo root, never in code. With no
``.env`` and no environment variables set, :func:`load_settings` returns a config
whose :attr:`Settings.llm_ready` is ``False`` — so the whole system stays offline
on deterministic fakes and spends nothing. Real Claude calls happen only when a
key is present *and* ``SENTINEL_USE_LLM`` is truthy.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOTENV = _REPO_ROOT / ".env"
_DEFAULT_MODEL = "claude-haiku-4-5"  # the cheapest model ($1/$5 per 1M tokens)
_TRUTHY = {"1", "true", "yes", "on"}


def _parse_dotenv(path: Path) -> dict[str, str]:
    """Minimal, dependency-free ``KEY=value`` parser (ignores blanks and ``#`` comments)."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


@dataclass(frozen=True)
class Settings:
    """Resolved runtime configuration."""

    anthropic_api_key: str | None
    llm_model: str
    use_llm: bool

    @property
    def llm_ready(self) -> bool:
        """True only when LLM mode is on AND a key is present — else stay on fakes."""
        return self.use_llm and bool(self.anthropic_api_key)


def load_settings(
    *, env: Mapping[str, str] | None = None, dotenv_path: Path | None = None
) -> Settings:
    """Load settings from the process environment, falling back to the ``.env`` file.

    Real environment variables take precedence over ``.env`` entries. Injectable
    ``env`` / ``dotenv_path`` make this fully testable without touching real config.
    """
    file_values = _parse_dotenv(_DOTENV if dotenv_path is None else dotenv_path)
    process = dict(os.environ if env is None else env)
    merged = {**file_values, **process}  # process env overrides the .env file
    return Settings(
        anthropic_api_key=merged.get("ANTHROPIC_API_KEY") or None,
        llm_model=merged.get("SENTINEL_LLM_MODEL") or _DEFAULT_MODEL,
        use_llm=merged.get("SENTINEL_USE_LLM", "0").strip().lower() in _TRUTHY,
    )
