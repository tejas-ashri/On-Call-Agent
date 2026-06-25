"""Settings + the Claude-key resolution (doc 05, doc 07).

Proves the default-offline guarantee: with no key and no flag, ``llm_ready`` is
False and the system never reaches for the network. Both the key and the flag are
required to go live, and the gitignored ``.env`` file is read as a fallback.
"""

from __future__ import annotations

from pathlib import Path

from sentinel.config import load_settings

_NO_FILE = Path("/nonexistent/.env")


def test_offline_by_default() -> None:
    s = load_settings(env={}, dotenv_path=_NO_FILE)
    assert s.anthropic_api_key is None
    assert s.use_llm is False
    assert s.llm_ready is False
    assert s.llm_model == "claude-haiku-4-5"  # cheapest by default


def test_llm_ready_requires_both_key_and_flag() -> None:
    s = load_settings(
        env={"ANTHROPIC_API_KEY": "sk-test", "SENTINEL_USE_LLM": "1"}, dotenv_path=_NO_FILE
    )
    assert s.llm_ready is True


def test_flag_without_key_stays_offline() -> None:
    s = load_settings(env={"SENTINEL_USE_LLM": "1"}, dotenv_path=_NO_FILE)
    assert s.llm_ready is False


def test_key_without_flag_stays_offline() -> None:
    s = load_settings(env={"ANTHROPIC_API_KEY": "sk-test"}, dotenv_path=_NO_FILE)
    assert s.use_llm is False
    assert s.llm_ready is False


def test_dotenv_file_is_read(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        'ANTHROPIC_API_KEY="sk-from-file"\n'
        "SENTINEL_USE_LLM=yes\n"
        "SENTINEL_LLM_MODEL=claude-sonnet-4-6\n"
        "# a comment line\n"
    )
    s = load_settings(env={}, dotenv_path=dotenv)
    assert s.anthropic_api_key == "sk-from-file"
    assert s.use_llm is True
    assert s.llm_model == "claude-sonnet-4-6"
    assert s.llm_ready is True


def test_process_env_overrides_dotenv(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("SENTINEL_LLM_MODEL=claude-haiku-4-5\n")
    s = load_settings(env={"SENTINEL_LLM_MODEL": "claude-opus-4-8"}, dotenv_path=dotenv)
    assert s.llm_model == "claude-opus-4-8"
