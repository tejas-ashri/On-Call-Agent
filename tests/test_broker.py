"""Tests for the MCP Router + JIT credential broker (doc 05 §2, ADR-005).

The security spine. These tests pin the ONE invariant: the ``tenant_id`` that
selects the credential comes from the graph's FROZEN server-side identity, never
from call arguments the LLM can influence (the confused-deputy fix, doc 05 §2).

No ``pytest-asyncio``: the coroutines are driven with ``asyncio.run`` from plain
sync test functions. Vault/audit/server dependencies are in-memory FAKES that
record their calls so we can assert on the exact arguments brokered.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta

import pytest
from pydantic import ValidationError

from sentinel.mcp.broker import (
    BrokerRequest,
    Credential,
    MCPRouter,
    ToolBroker,
    ToolResult,
)

# The secret value must NEVER appear in args, audit detail, or the result.
_SECRET = "super-secret-token-DO-NOT-LEAK"


@dataclass
class MintCall:
    """A recorded ``mint_scoped`` invocation (precise types so tests can index)."""

    tenant_id: str
    provider: str
    scope: str
    ttl: timedelta


@dataclass
class FakeVault:
    """Records every mint/revoke; hands back a deterministic Credential."""

    minted: list[MintCall] = field(default_factory=list)
    revoked: list[str] = field(default_factory=list)
    next_lease: str = "lease_001"

    async def mint_scoped(
        self, *, tenant_id: str, provider: str, scope: str, ttl: timedelta
    ) -> Credential:
        self.minted.append(
            MintCall(tenant_id=tenant_id, provider=provider, scope=scope, ttl=ttl)
        )
        return Credential(
            lease_id=self.next_lease, secret=_SECRET, ttl_seconds=int(ttl.total_seconds())
        )

    async def revoke(self, lease_id: str) -> None:
        self.revoked.append(lease_id)


@dataclass
class AuditEntry:
    """A recorded ``append`` invocation (precise types so tests can index)."""

    tenant_id: str
    actor: str
    action: str
    target: str
    detail: dict[str, object]


@dataclass
class FakeAudit:
    """Records every append so we can assert what the chain would record."""

    entries: list[AuditEntry] = field(default_factory=list)

    async def append(
        self,
        *,
        tenant_id: str,
        actor: str,
        action: str,
        target: str,
        detail: dict[str, object],
    ) -> None:
        self.entries.append(
            AuditEntry(
                tenant_id=tenant_id, actor=actor, action=action, target=target, detail=detail
            )
        )


@dataclass
class InvokeCall:
    """A recorded ``invoke`` invocation (precise types so tests can index)."""

    action: str
    target: str
    args: dict[str, object]
    credential: str


@dataclass
class FakeServer:
    """Records each invoke; echoes the credential it saw so leak-checks can run."""

    calls: list[InvokeCall] = field(default_factory=list)
    raise_on_invoke: bool = False

    async def invoke(
        self, *, action: str, target: str, args: dict[str, object], credential: str
    ) -> ToolResult:
        self.calls.append(
            InvokeCall(action=action, target=target, args=args, credential=credential)
        )
        if self.raise_on_invoke:
            raise RuntimeError("upstream MCP server blew up")
        return ToolResult(ok=True, data={"saw_credential": credential, "echo_target": target})


def _router(
    vault: FakeVault, audit: FakeAudit, server: FakeServer, provider: str = "github"
) -> MCPRouter:
    return MCPRouter(vault=vault, audit=audit, servers={provider: server})


# --------------------------------------------------------------------------- #
# Happy path: mint -> invoke -> revoke -> audit, with the secret NEVER leaking.
# --------------------------------------------------------------------------- #
def test_broker_mints_invokes_revokes_then_audits() -> None:
    vault, audit, server = FakeVault(), FakeAudit(), FakeServer()
    router = _router(vault, audit, server)
    req = BrokerRequest(
        tenant_id="ten_A", incident_id="inc_1", provider="github",
        action="repo.read", target="svc_api",
    )

    result = asyncio.run(router.broker(req, {"path": "/README.md"}))

    assert result.ok is True
    # Minted once, against the request tenant, with a least-priv read scope + 30s TTL.
    assert len(vault.minted) == 1
    mint = vault.minted[0]
    assert mint.tenant_id == "ten_A"
    assert mint.provider == "github"
    assert mint.ttl == timedelta(seconds=30)
    # The server was invoked exactly once, WITH the injected secret in the credential slot.
    assert len(server.calls) == 1
    assert server.calls[0].credential == _SECRET
    assert server.calls[0].args == {"path": "/README.md"}
    # Lease was revoked.
    assert vault.revoked == ["lease_001"]
    # Exactly one audit entry was appended.
    assert len(audit.entries) == 1


def test_write_call_gets_longer_ttl_and_records_write_flag() -> None:
    vault, audit, server = FakeVault(), FakeAudit(), FakeServer()
    router = _router(vault, audit, server)
    req = BrokerRequest(
        tenant_id="ten_A", incident_id="inc_1", provider="github",
        action="repo.write", target="svc_api", write=True,
    )

    asyncio.run(router.broker(req, {}))

    # Write calls draw a longer (60s) TTL per doc 05 §2.2.
    assert vault.minted[0].ttl == timedelta(seconds=60)
    assert audit.entries[0].detail["write"] is True


def test_audit_detail_carries_lease_and_scope_but_never_the_secret() -> None:
    vault, audit, server = FakeVault(), FakeAudit(), FakeServer()
    router = _router(vault, audit, server)
    req = BrokerRequest(
        tenant_id="ten_A", incident_id="inc_42", provider="github",
        action="repo.read", target="svc_api",
    )

    asyncio.run(router.broker(req, {"q": "boom"}))

    entry = audit.entries[0]
    detail = entry.detail
    # detail carries the spend metadata...
    assert detail["lease_id"] == "lease_001"
    assert detail["provider"] == "github"
    assert "scope" in detail
    assert detail["write"] is False
    # actor / action / target are recorded for the chain.
    assert entry.tenant_id == "ten_A"
    assert entry.actor == "router:inc_42"
    assert entry.action == "tool.repo.read"
    assert entry.target == "svc_api"
    # ...but the secret appears NOWHERE in the audit entry (C2, doc 05 §2.2).
    assert _SECRET not in _flatten(entry)
    # And the secret is not handed back to the graph in any args either.
    assert _SECRET not in _flatten(server.calls[0].args)
    # The broker never injects the secret into args or audit — the two stores the
    # LLM/graph and durable systems can see.
    assert _SECRET not in _flatten(audit.entries)


# --------------------------------------------------------------------------- #
# THE CONFUSED-DEPUTY TEST (doc 05 §2): a ToolBroker bound to ten_A, called with
# **args carrying tenant_id="ten_B", STILL mints against ten_A. The foreign
# tenant_id is an inert tool argument, not an input to credential selection.
# --------------------------------------------------------------------------- #
def test_confused_deputy_foreign_tenant_id_in_args_is_inert() -> None:
    vault, audit, server = FakeVault(), FakeAudit(), FakeServer()
    router = _router(vault, audit, server)
    # Broker is closed over ten_A's frozen identity (server-side, pre-LLM).
    broker = ToolBroker(tenant_id="ten_A", incident_id="inc_A", router=router)

    # A prompt-injected / buggy LLM tries to smuggle a foreign tenant_id through
    # the tool-call args. It is just data.
    result = asyncio.run(
        broker.call(
            provider="github",
            action="repo.read",
            target="svc_victim",
            tenant_id="ten_B",  # <-- the attack: foreign tenant in **args
            note="please act as ten_B",
        )
    )

    assert result.ok is True
    # THE INVARIANT: Vault was asked to mint for ten_A (the closure), NOT ten_B.
    assert len(vault.minted) == 1
    minted_tenant = vault.minted[0].tenant_id
    assert minted_tenant == "ten_A"
    # The credential is bound to the closure's tenant, not the smuggled argument.
    smuggled = "ten_B"
    assert minted_tenant != smuggled
    # The foreign tenant_id survives only as an inert argument forwarded to the
    # MCP server — it never touched credential selection.
    assert server.calls[0].args["tenant_id"] == "ten_B"
    # The audit entry is attributed to ten_A, never ten_B.
    assert audit.entries[0].tenant_id == "ten_A"


def test_two_brokers_each_mint_against_their_own_tenant() -> None:
    vault, audit, server = FakeVault(), FakeAudit(), FakeServer()
    router = _router(vault, audit, server)
    broker_a = ToolBroker(tenant_id="ten_A", incident_id="inc_A", router=router)
    broker_b = ToolBroker(tenant_id="ten_B", incident_id="inc_B", router=router)

    asyncio.run(broker_a.call(provider="github", action="repo.read", target="svc_1"))
    asyncio.run(broker_b.call(provider="github", action="repo.read", target="svc_2"))

    minted_tenants = [m.tenant_id for m in vault.minted]
    assert minted_tenants == ["ten_A", "ten_B"]
    audited_tenants = [e.tenant_id for e in audit.entries]
    assert audited_tenants == ["ten_A", "ten_B"]


def test_toolbroker_tenant_id_is_frozen_no_setter() -> None:
    vault, audit, server = FakeVault(), FakeAudit(), FakeServer()
    router = _router(vault, audit, server)
    broker = ToolBroker(tenant_id="ten_A", incident_id="inc_A", router=router)

    # The frozen identity is private; there is no public ``tenant_id`` attribute and
    # no setter (doc 05 §2.2). Assigning ``broker.tenant_id`` would create an
    # unrelated attribute — it can NEVER reach the closure used to build the
    # BrokerRequest, so credential selection stays pinned to ten_A.
    assert not hasattr(broker, "tenant_id")
    broker.tenant_id = "ten_B"  # type: ignore[attr-defined]  # inert: not the closure
    asyncio.run(broker.call(provider="github", action="repo.read", target="svc_1"))
    assert vault.minted[0].tenant_id == "ten_A"


# --------------------------------------------------------------------------- #
# revoke is ALWAYS called, even if invoke raises (finally-block guarantee).
# --------------------------------------------------------------------------- #
def test_revoke_always_called_even_when_invoke_raises() -> None:
    vault, audit, server = FakeVault(), FakeAudit(), FakeServer(raise_on_invoke=True)
    router = _router(vault, audit, server)
    req = BrokerRequest(
        tenant_id="ten_A", incident_id="inc_1", provider="github",
        action="repo.read", target="svc_api",
    )

    with pytest.raises(RuntimeError, match="upstream MCP server blew up"):
        asyncio.run(router.broker(req, {}))

    # The mint happened, the invoke raised, but revoke STILL ran (doc 05 §2.2).
    assert len(vault.minted) == 1
    assert vault.revoked == ["lease_001"]


def test_brokerrequest_is_frozen_and_forbids_extra() -> None:
    req = BrokerRequest(
        tenant_id="ten_A", incident_id="inc_1", provider="github",
        action="repo.read", target="svc_api",
    )
    # frozen: no mutation after construction (the confused-deputy fix, doc 05 §2.2).
    with pytest.raises(ValidationError):
        req.tenant_id = "ten_B"  # type: ignore[misc]
    # extra="forbid": a stray field (e.g. a smuggled credential) is rejected.
    with pytest.raises(ValidationError):
        BrokerRequest(
            tenant_id="ten_A", incident_id="inc_1", provider="github",
            action="repo.read", target="svc_api", secret="x",  # type: ignore[call-arg]
        )


def test_scope_is_least_privilege_read_vs_write() -> None:
    vault, audit, server = FakeVault(), FakeAudit(), FakeServer()
    router = _router(vault, audit, server)
    read_req = BrokerRequest(
        tenant_id="ten_A", incident_id="inc_1", provider="github",
        action="repo.read", target="svc_api",
    )
    write_req = BrokerRequest(
        tenant_id="ten_A", incident_id="inc_1", provider="github",
        action="repo.write", target="svc_api", write=True,
    )

    asyncio.run(router.broker(read_req, {}))
    asyncio.run(router.broker(write_req, {}))

    read_scope = vault.minted[0].scope
    write_scope = vault.minted[1].scope
    # Read and write derive DIFFERENT scopes server-side; neither is caller-supplied.
    assert read_scope != write_scope
    assert "read" in read_scope
    assert "write" in write_scope


def test_credential_and_toolresult_defaults() -> None:
    cred = Credential(lease_id="lease_x", secret="s", ttl_seconds=30)
    assert cred.ttl_seconds == 30
    # ToolResult.data defaults to an empty dict (not a shared mutable).
    r1, r2 = ToolResult(ok=True), ToolResult(ok=False)
    assert r1.data == {} and r2.data == {}
    r1.data["k"] = "v"
    assert r2.data == {}  # no shared-default aliasing


def _flatten(obj: object) -> str:
    """Stringify a nested structure so we can substring-search for a leaked secret."""
    return repr(obj)
