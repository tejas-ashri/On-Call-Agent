"""Provision the dev database: roles, schema, and (optionally) the RLS policies.

Uses raw asyncpg on the *simple* query protocol so multi-statement scripts and ``DO $$``
blocks execute in one shot — SQLAlchemy/asyncpg's prepared protocol rejects both. Run as
a module to set everything up::

    PYTHONPATH=src ./.venv/bin/python -m sentinel.data.bootstrap

The split between schema and policies is deliberate: ``setup(with_policies=False)`` leaves
the tables wide open so the isolation test can prove it catches a breach *before* RLS,
then ``apply_policies()`` closes it (RED → GREEN).
"""

from __future__ import annotations

import asyncio
import getpass
import os
from pathlib import Path

import asyncpg

_DIR = Path(__file__).resolve().parent
_DB_NAME = os.environ.get("SENTINEL_DB_NAME", "sentinel_dev")
_HOST = os.environ.get("SENTINEL_DB_HOST", "127.0.0.1")
_PORT = int(os.environ.get("SENTINEL_DB_PORT", "5432"))


def _superuser() -> str:
    return os.environ.get("USER") or getpass.getuser()


def _sql(name: str) -> str:
    return (_DIR / name).read_text(encoding="utf-8")


async def _connect(database: str) -> asyncpg.Connection:
    return await asyncpg.connect(host=_HOST, port=_PORT, user=_superuser(), database=database)


async def _run(sql: str) -> None:
    conn = await _connect(_DB_NAME)
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


async def ensure_database() -> None:
    conn = await _connect("postgres")
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", _DB_NAME)
        if not exists:
            await conn.execute(f'CREATE DATABASE "{_DB_NAME}"')
    finally:
        await conn.close()


async def apply_roles() -> None:
    await _run(_sql("roles.sql"))


async def apply_schema() -> None:
    await _run(_sql("schema.sql"))


async def apply_grants() -> None:
    await _run(_sql("grants.sql"))


async def apply_policies() -> None:
    await _run(_sql("policies.sql"))


async def setup(*, with_policies: bool = True) -> None:
    await ensure_database()
    await apply_roles()
    await apply_schema()
    await apply_grants()  # verbs are always granted; RLS (policies) decides the rows
    if with_policies:
        await apply_policies()


def main() -> None:
    asyncio.run(setup(with_policies=True))
    print(f"sentinel: bootstrapped '{_DB_NAME}' — roles + schema + RLS policies applied.")


if __name__ == "__main__":
    main()
