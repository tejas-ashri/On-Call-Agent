"""Async engine + the per-request tenant scope (doc 02 §3.5).

``tenant_session`` opens a transaction whose every statement is RLS-scoped to one
tenant by setting the ``app.tenant_id`` GUC with ``SET LOCAL`` semantics
(``set_config(..., true)``). ``SET LOCAL`` ties the GUC to *this* transaction and
auto-resets on commit/rollback — essential under pooled connections, where a leaked
GUC would itself be a cross-tenant leak. ``set_config`` is parameter-safe: a hostile
``tenant_id`` is always a value, never SQL.

Engines use ``NullPool`` so no connection is retained across event loops — this lets a
fresh engine be created inside each ``asyncio.run`` (the project's test style) without
"future attached to a different loop" errors.
"""

from __future__ import annotations

import getpass
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool


def _superuser() -> str:
    return os.environ.get("USER") or getpass.getuser()


# Plain libpq DSN (no +asyncpg) for bootstrap DDL via raw asyncpg — see bootstrap.py.
SUPERUSER_PG_DSN = os.environ.get(
    "SENTINEL_DB_SUPERUSER_DSN",
    f"postgresql://{_superuser()}@127.0.0.1:5432/sentinel_dev",
)
# SQLAlchemy URL the application connects with: the NOBYPASSRLS runtime role.
APP_DSN = os.environ.get(
    "SENTINEL_DB_APP_DSN",
    "postgresql+asyncpg://sentinel_runtime:sentinel@127.0.0.1:5432/sentinel_dev",
)


def make_engine(dsn: str = APP_DSN) -> AsyncEngine:
    """Create an async engine that holds no cross-loop connection state."""
    return create_async_engine(dsn, poolclass=NullPool)


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def tenant_session(
    factory: async_sessionmaker[AsyncSession], tenant_id: str
) -> AsyncIterator[AsyncSession]:
    """Open a transaction whose every statement is RLS-scoped to ``tenant_id``.

    The GUC is set with ``SET LOCAL`` (``set_config(..., true)``) so it is gone on
    commit/rollback and cannot ride a pooled connection into another tenant's request.
    """
    async with factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(tenant_id)},
        )
        yield session
        # commit (on context exit) resets app.tenant_id automatically
