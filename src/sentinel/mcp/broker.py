"""MCP Router + JIT credential broker — the security spine (doc 05 §2, ADR-005).

This module solves the **confused-deputy problem** (doc 05 §2). The router acts on
behalf of many tenants and can mint the authority of all of them; a confused deputy
is one *tricked* into using tenant-A's authority for tenant-B's request. Because a
request is partly shaped by an LLM reasoning over tenant data, the threat is concrete.

The fix is *structural, not validation*: the ``tenant_id`` that selects the Vault
credential path comes from the server-side :class:`ToolBroker` closure — frozen when
the orchestrator spawned the graph (doc 04 §1) — **never** from call arguments the
LLM can influence. The LLM supplies *what* to do (``provider``/``action``/``target``);
never *whose* credential. So tenant-B's graph physically cannot construct a
:class:`BrokerRequest` for tenant-A: cross-tenant tool access is *unrepresentable*.

Two further properties (doc 05 §2.2): ``cred.secret`` exists only inside
:meth:`MCPRouter.broker` — never returned to the graph, never placed in ``args``,
never serialized into the audit ``detail``; and the lease is revoked in a ``finally``
block so a raising tool call still bounds exposure (the short TTL is the real
guarantee, P9).

All external dependencies (Vault / audit / MCP servers) are injected :class:`Protocol`
implementations; this module is offline and pure, with in-memory fakes used in tests.
"""

from __future__ import annotations

import contextlib
from datetime import timedelta
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

# Server-side TTL clamps (doc 05 §2.2): reads are cheap and short, writes draw
# extra scrutiny and a slightly longer window. Neither is caller-supplied.
_READ_TTL = timedelta(seconds=30)
_WRITE_TTL = timedelta(seconds=60)


class Credential(BaseModel):
    """A short-lived, scoped credential minted by Vault for a single tool call.

    ``secret`` is the live credential material; per doc 05 §2.2 it is injected only
    into the MCP server call's credential slot and never enters ``args``, the audit
    ``detail``, logs, or the LLM trace. ``lease_id`` is the safe-to-log handle used
    for revocation and the audit record.
    """

    model_config = ConfigDict(extra="forbid")

    lease_id: str
    secret: str
    ttl_seconds: int


class ToolResult(BaseModel):
    """The outcome of an MCP tool invocation (doc 05 §2.2)."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    data: dict[str, object] = Field(default_factory=dict)


class BrokerRequest(BaseModel):
    """A request to spend authority (doc 05 §2.2).

    ``tenant_id`` is set SERVER-SIDE from the graph's frozen identity (via the
    :class:`ToolBroker` closure) — never from LLM output. This is the confused-deputy
    fix. ``target`` is an *argument* describing the resource to act on, not authority:
    naming a foreign resource cannot redirect which tenant's credential is minted.

    ``frozen=True`` makes the request immutable once built, so no in-process mutation
    can swap the tenant after construction.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str  # ten_… — from the ToolBroker closure, NOT from args
    incident_id: str
    provider: str  # 'github', 'vercel', … (CanonicalEvent.source)
    action: str  # tool verb, e.g. 'repo.read'
    target: str  # svc_… / resource — an ARGUMENT, not authority
    write: bool = False  # write calls draw extra scrutiny + audit


class VaultClient(Protocol):
    """Injected Vault facade (doc 05 §3). The router's identity can only *mint*
    per-tenant scoped child credentials; it can never read a stored secret directly."""

    async def mint_scoped(
        self, *, tenant_id: str, provider: str, scope: str, ttl: timedelta
    ) -> Credential:
        """Mint a short-lived credential at the tenant's Vault path (doc 05 §3.2)."""
        ...

    async def revoke(self, lease_id: str) -> None:
        """Revoke a lease best-effort; the short TTL is the real guarantee (P9)."""
        ...


class AuditSink(Protocol):
    """Injected append-only audit sink — the tamper-evident per-tenant chain (doc 05 §5).

    Records *authority spent*, never *data seen*: ``detail`` carries ``lease_id`` and
    ``scope`` but never a credential or secret (doc 05 §5.2)."""

    async def append(
        self,
        *,
        tenant_id: str,
        actor: str,
        action: str,
        target: str,
        detail: dict[str, object],
    ) -> None: ...


class MCPServer(Protocol):
    """Injected MCP connector server (e.g. connector-github, doc 05 §2.1).

    The credential is injected into the invocation's credential slot (conceptually
    request headers) — never echoed back, never logged."""

    async def invoke(
        self, *, action: str, target: str, args: dict[str, object], credential: str
    ) -> ToolResult: ...


def scope_for(provider: str, action: str, target: str, *, write: bool) -> str:
    """Derive a least-privilege Vault scope SERVER-SIDE from the request's *type*.

    Per doc 05 §2 / §3.2 the caller cannot request a broader scope: scope is a
    property of ``(provider, action, target, write)``, computed here, never supplied
    by the LLM. Write requests yield a strictly different (write) scope than reads.
    """

    capability = "write" if write else "read"
    return f"{provider}:{capability}:{target}"


class MCPRouter:
    """Routes a tenant-bound :class:`BrokerRequest` to the right MCP server, minting
    and injecting a JIT credential for the duration of exactly one call (doc 05 §2.2).
    Holds no standing credential of its own (no ambient authority, doc 05 §2)."""

    def __init__(
        self,
        vault: VaultClient,
        audit: AuditSink,
        servers: dict[str, MCPServer],
    ) -> None:
        self._vault = vault
        self._audit = audit
        self._servers = servers

    async def broker(self, req: BrokerRequest, args: dict[str, object]) -> ToolResult:
        """Mint → inject → invoke → revoke → audit, for one tool call (doc 05 §2.2).

        The credential is minted against ``req.tenant_id`` (the graph's frozen
        identity); ``cred.secret`` is injected only into the server call and never
        leaks into ``args``, the audit ``detail``, or logs (doc 05 §2.2).
        """

        # 1) Server-side least-privilege scope + short TTL, derived from request type.
        scope = scope_for(req.provider, req.action, req.target, write=req.write)
        ttl = _WRITE_TTL if req.write else _READ_TTL

        # 2) Mint a scoped, short-lived credential from the TENANT's Vault path.
        #    The path is keyed on req.tenant_id — tenant-B cannot reach ten_A's path
        #    because req.tenant_id is bound to the graph closure (confused-deputy fix).
        cred = await self._vault.mint_scoped(
            tenant_id=req.tenant_id, provider=req.provider, scope=scope, ttl=ttl
        )
        try:
            server = self._servers[req.provider]
            # 3) Inject the credential into the tool invocation. It lives ONLY in this
            #    call's credential slot — never in args, never in logs, never in the
            #    LLM trace (C2, doc 05 §2.2).
            result = await server.invoke(
                action=req.action,
                target=req.target,
                args=args,
                credential=cred.secret,  # injected here, used once
            )
        finally:
            # 4) Revoke immediately (best-effort); the short TTL is the real
            #    guarantee (P9). Suppression keeps a revoke failure from masking the
            #    original outcome (or the original exception).
            with contextlib.suppress(Exception):
                await self._vault.revoke(cred.lease_id)
            # 5) Audit the spend — with the lease_id, NEVER the secret. Hash-chained
            #    per-tenant (doc 05 §5). Runs even if invoke raised, so every mint is
            #    accounted for.
            await self._audit.append(
                tenant_id=req.tenant_id,
                actor=f"router:{req.incident_id}",
                action=f"tool.{req.action}",
                target=req.target,
                detail={
                    "provider": req.provider,
                    "lease_id": cred.lease_id,
                    "scope": scope,
                    "write": req.write,
                },  # redacted; no credential
            )
        return result


class ToolBroker:
    """Constructed once per incident graph, closed over a FROZEN ``tenant_id`` and
    ``incident_id`` (doc 04 §1 / doc 05 §2). A graph holds exactly one of these; it is
    the only way the graph can reach a tool.

    The frozen identity has no setter — :attr:`_tenant_id` is private and never
    mutated, so no in-process code (and crucially no LLM-supplied argument) can swap
    the tenant after construction.
    """

    def __init__(self, *, tenant_id: str, incident_id: str, router: MCPRouter) -> None:
        self._tenant_id = tenant_id  # frozen identity; no setter exists
        self._incident_id = incident_id
        self._router = router

    async def call(
        self,
        *,
        provider: str,
        action: str,
        target: str,
        write: bool = False,
        **args: object,
    ) -> ToolResult:
        """Build a :class:`BrokerRequest` and broker it (doc 05 §2.2).

        ``tenant_id`` comes from SELF (the closure), not from ``**args`` — so a
        foreign ``tenant_id`` smuggled into ``args`` by a prompt-injected LLM is an
        inert tool argument, forwarded to the MCP server but never able to influence
        which tenant's credential is minted (the confused-deputy fix, doc 05 §2).
        """

        req = BrokerRequest(
            tenant_id=self._tenant_id,
            incident_id=self._incident_id,
            provider=provider,
            action=action,
            target=target,
            write=write,
        )
        return await self._router.broker(req, args)
