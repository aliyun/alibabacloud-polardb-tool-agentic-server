from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from server.config import PolarRAGToolLimitsConfig
from server.polarrag.access import KnowledgeResourceScope
from server.core.polarrag_governance import (
    PolarRAGGovernanceError,
    PolarRAGToolGovernor,
    set_polarrag_governance_metric_sink,
)


class MutableConfig:
    def __init__(self, value: PolarRAGToolLimitsConfig) -> None:
        self.value = value

    def __call__(self) -> PolarRAGToolLimitsConfig:
        return self.value


@pytest.fixture(autouse=True)
def reset_metric_sink():
    set_polarrag_governance_metric_sink(None)
    yield
    set_polarrag_governance_metric_sink(None)


async def test_dual_scope_rejection_does_not_consume_passing_user_bucket() -> None:
    config = MutableConfig(
        PolarRAGToolLimitsConfig(
            user_requests_per_minute=1,
            user_burst=2,
            agent_requests_per_minute=1,
            agent_burst=1,
        )
    )
    governor = PolarRAGToolGovernor(config)

    await governor.check_rate("kb_search", "user-a", "agent-a")
    with pytest.raises(PolarRAGGovernanceError) as rejected:
        await governor.check_rate("kb_search", "user-a", "agent-a")
    await governor.check_rate("kb_search", "user-a", "agent-b")

    assert rejected.value.public_reason == "RATE_LIMIT"
    assert rejected.value.internal_reason == "agent_rate_limit"


async def test_rate_buckets_are_isolated_by_user_and_agent() -> None:
    config = MutableConfig(
        PolarRAGToolLimitsConfig(
            user_requests_per_minute=1,
            user_burst=1,
            agent_requests_per_minute=1,
            agent_burst=1,
        )
    )
    governor = PolarRAGToolGovernor(config)

    await governor.check_rate("doc_status", "user-a", "agent-a")
    await governor.check_rate("doc_status", "user-b", "agent-b")

    with pytest.raises(PolarRAGGovernanceError) as user_rejected:
        await governor.check_rate("doc_status", "user-a", "agent-b")
    assert user_rejected.value.internal_reason == "user_rate_limit"


async def test_rate_bucket_identity_churn_is_bounded() -> None:
    governor = PolarRAGToolGovernor(
        MutableConfig(PolarRAGToolLimitsConfig()),
        max_rate_buckets=32,
    )

    capacity_rejections = 0
    for index in range(100):
        try:
            await governor.check_rate(
                "kb_search",
                f"user-{index}",
                f"agent-{index}",
            )
        except PolarRAGGovernanceError as exc:
            assert exc.internal_reason == "rate_bucket_capacity"
            capacity_rejections += 1

    assert governor.rate_bucket_count <= 32
    assert capacity_rejections > 0


async def test_bucket_capacity_does_not_evict_exhausted_identities() -> None:
    governor = PolarRAGToolGovernor(
        MutableConfig(
            PolarRAGToolLimitsConfig(
                user_requests_per_minute=1,
                user_burst=1,
                agent_requests_per_minute=1,
                agent_burst=1,
            )
        ),
        clock=lambda: 100.0,
        max_rate_buckets=2,
    )

    await governor.check_rate("kb_search", "user-a", "agent-a")
    with pytest.raises(PolarRAGGovernanceError) as capacity_rejected:
        await governor.check_rate("kb_search", "user-b", "agent-b")
    with pytest.raises(PolarRAGGovernanceError) as original_rejected:
        await governor.check_rate("kb_search", "user-a", "agent-a")

    assert capacity_rejected.value.internal_reason == "rate_bucket_capacity"
    assert original_rejected.value.internal_reason == "user_rate_limit"
    assert governor.rate_bucket_count == 2


async def test_idle_full_rate_buckets_are_swept() -> None:
    now = [100.0]
    governor = PolarRAGToolGovernor(
        MutableConfig(
            PolarRAGToolLimitsConfig(
                user_requests_per_minute=60,
                user_burst=1,
            )
        ),
        clock=lambda: now[0],
        bucket_sweep_interval_seconds=60,
    )

    await governor.check_rate("kb_search", "old-user", None)
    now[0] += 61
    await governor.check_rate("kb_search", "new-user", None)

    assert governor.rate_bucket_count == 1


async def test_rate_retry_after_uses_refill_time() -> None:
    now = [100.0]
    config = MutableConfig(
        PolarRAGToolLimitsConfig(
            user_requests_per_minute=60,
            user_burst=1,
        )
    )
    governor = PolarRAGToolGovernor(config, clock=lambda: now[0])

    await governor.check_rate("kb_search", "user-a", None)
    with pytest.raises(PolarRAGGovernanceError) as rejected:
        await governor.check_rate("kb_search", "user-a", None)
    assert rejected.value.retry_after_seconds == 1

    now[0] += 1.0
    await governor.check_rate("kb_search", "user-a", None)


async def test_rate_retry_after_waits_for_all_rejected_scopes() -> None:
    governor = PolarRAGToolGovernor(
        MutableConfig(
            PolarRAGToolLimitsConfig(
                user_requests_per_minute=120,
                user_burst=1,
                agent_requests_per_minute=1,
                agent_burst=1,
            )
        ),
        clock=lambda: 100.0,
    )

    await governor.check_rate("kb_search", "user-a", "agent-a")
    with pytest.raises(PolarRAGGovernanceError) as rejected:
        await governor.check_rate("kb_search", "user-a", "agent-a")

    assert rejected.value.retry_after_seconds == 60


async def test_configuration_change_and_disable_apply_without_restart() -> None:
    config = MutableConfig(
        PolarRAGToolLimitsConfig(
            user_requests_per_minute=1,
            user_burst=1,
        )
    )
    governor = PolarRAGToolGovernor(config)

    await governor.check_rate("kb_search", "user-a", None)
    with pytest.raises(PolarRAGGovernanceError):
        await governor.check_rate("kb_search", "user-a", None)

    config.value = config.value.model_copy(update={"user_burst": 2})
    await governor.check_rate("kb_search", "user-a", None)
    await governor.check_rate("kb_search", "user-a", None)

    config.value = config.value.model_copy(update={"enabled": False})
    for _ in range(5):
        await governor.check_rate("kb_search", "user-a", None)

    config.value = config.value.model_copy(update={"enabled": True})
    await governor.check_rate("kb_search", "user-a", None)


async def test_instance_reservation_is_atomic_and_isolated() -> None:
    config = MutableConfig(
        PolarRAGToolLimitsConfig(
            instance_max_inflight=2,
            max_fanout=2,
            retry_after_seconds=4,
        )
    )
    governor = PolarRAGToolGovernor(config)

    async with governor.reserve_instance("kb_search", "instance-a", 2):
        with pytest.raises(PolarRAGGovernanceError) as rejected:
            async with governor.reserve_instance("doc_status", "instance-a", 1):
                raise AssertionError("rejected reservation entered")
        assert rejected.value.public_reason == "INSTANCE_CONCURRENCY"
        assert rejected.value.retry_after_seconds == 4
        async with governor.reserve_instance("doc_status", "instance-b", 2):
            pass

    async with governor.reserve_instance("kb_search", "instance-a", 2):
        pass


async def test_blocked_instance_does_not_block_another_instance() -> None:
    governor = PolarRAGToolGovernor(
        MutableConfig(
            PolarRAGToolLimitsConfig(
                instance_max_inflight=1,
                max_fanout=1,
            )
        )
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def hold_instance() -> None:
        async with governor.reserve_instance("kb_search", "instance-a", 1):
            started.set()
            await release.wait()

    holder = asyncio.create_task(hold_instance())
    await started.wait()
    try:
        with pytest.raises(PolarRAGGovernanceError):
            async with governor.reserve_instance("doc_status", "instance-a", 1):
                raise AssertionError("same-instance reservation entered")
        async with governor.reserve_instance("doc_status", "instance-b", 1):
            pass
    finally:
        release.set()
        await holder


async def test_fanout_rejects_before_entering_reservation() -> None:
    governor = PolarRAGToolGovernor(MutableConfig(PolarRAGToolLimitsConfig(max_fanout=2)))

    with pytest.raises(PolarRAGGovernanceError) as rejected:
        async with governor.reserve_instance("kb_search", "instance-a", 3):
            raise AssertionError("fanout rejection entered")

    assert rejected.value.public_reason == "FANOUT_LIMIT"
    assert rejected.value.requested_fanout == 3


async def test_reservation_releases_after_operation_failure() -> None:
    governor = PolarRAGToolGovernor(
        MutableConfig(
            PolarRAGToolLimitsConfig(
                instance_max_inflight=1,
                max_fanout=1,
            )
        )
    )

    with pytest.raises(RuntimeError, match="upstream failed"):
        async with governor.reserve_instance("doc_status", "instance-a", 1):
            raise RuntimeError("upstream failed")

    async with governor.reserve_instance("doc_status", "instance-a", 1):
        pass


async def test_disabled_governance_preserves_operation_failure() -> None:
    governor = PolarRAGToolGovernor(MutableConfig(PolarRAGToolLimitsConfig(enabled=False)))

    with pytest.raises(RuntimeError, match="upstream failed"):
        async with governor.reserve_instance("doc_status", "instance-a", 1):
            raise RuntimeError("upstream failed")


async def test_metrics_exclude_actor_and_raw_instance_identifiers() -> None:
    samples = []
    set_polarrag_governance_metric_sink(samples.append)
    governor = PolarRAGToolGovernor(
        MutableConfig(
            PolarRAGToolLimitsConfig(
                user_requests_per_minute=1,
                user_burst=1,
                instance_max_inflight=1,
                max_fanout=1,
            )
        )
    )

    await governor.check_rate("kb_search", "sensitive-user", None)
    with pytest.raises(PolarRAGGovernanceError):
        await governor.check_rate("kb_search", "sensitive-user", None)
    async with governor.reserve_instance("doc_status", "sensitive-instance", 1):
        pass

    rendered = repr(samples)
    assert "sensitive-user" not in rendered
    assert "sensitive-instance" not in rendered
    assert any(
        sample.name == "polarrag_tool_rejections_total"
        and sample.labels
        == {
            "tool": "kb_search",
            "reason": "user_rate_limit",
        }
        for sample in samples
    )
    inflight = [sample for sample in samples if sample.name == "polarrag_tool_instance_inflight"]
    assert [sample.value for sample in inflight] == [1.0, 0.0]
    assert inflight[0].labels == {"instance_scope": inflight[0].labels["instance_scope"]}
    assert len(inflight[0].labels["instance_scope"]) == 12


async def test_execute_tool_checks_user_and_agent_before_handler(
    monkeypatch,
) -> None:
    from server.mcp.tools import polarrag

    config = MutableConfig(
        PolarRAGToolLimitsConfig(
            user_requests_per_minute=1,
            user_burst=2,
            agent_requests_per_minute=1,
            agent_burst=1,
        )
    )
    governor = PolarRAGToolGovernor(config)
    user = SimpleNamespace(id="user-a")
    context = SimpleNamespace(
        user=user,
        agent=SimpleNamespace(id="agent-a"),
    )
    handler_calls = 0
    audit_calls = []

    @asynccontextmanager
    async def session_context():
        yield object()

    async def handler(_session, _user, **_kwargs):
        nonlocal handler_calls
        handler_calls += 1
        return polarrag._result({"items": []})

    async def log_audit(_session, **kwargs):
        audit_calls.append(kwargs)

    async def agent_context(_session):
        return context

    async def resource_scope(_session, **_kwargs):
        return KnowledgeResourceScope({"instance-a": None})

    async def build_audit(_session, _user, _resources, _payload, **_kwargs):
        return {"polarrag_status": "success"}

    monkeypatch.setattr(
        polarrag,
        "get_session_factory",
        lambda: session_context,
    )
    monkeypatch.setattr(polarrag, "current_agent_user_context", agent_context)
    monkeypatch.setattr(
        polarrag,
        "current_polarrag_resource_scope",
        resource_scope,
    )
    monkeypatch.setattr(polarrag, "log_audit", log_audit)
    monkeypatch.setattr(polarrag, "_build_audit_client_info", build_audit)
    monkeypatch.setattr(
        polarrag,
        "get_polarrag_tool_governor",
        lambda: governor,
    )

    first = await polarrag._execute_tool("kb_search", handler)
    rejected = await polarrag._execute_tool(
        "kb_search",
        handler,
        knowledge_resource_ids=["opaque-resource"],
    )

    assert first.isError is False
    assert handler_calls == 1
    assert json.loads(rejected.content[0].text)["reason"] == "RATE_LIMIT"
    assert audit_calls[-1]["error_code"] == "POLARRAG_TOOL_LIMITED"
    assert audit_calls[-1]["target_id"] is None
    assert "opaque-resource" not in audit_calls[-1]["client_info"]


async def test_execute_tool_audits_internal_governance_capacity(
    monkeypatch,
) -> None:
    from server.mcp.tools import polarrag

    user = SimpleNamespace(id="user-a")
    audit_calls = []

    @asynccontextmanager
    async def session_context():
        yield object()

    async def handler(_session, _user, **_kwargs):
        return polarrag._governance_error(
            PolarRAGGovernanceError(
                internal_reason="instance_concurrency",
                public_reason="INSTANCE_CONCURRENCY",
                retry_after_seconds=4,
                requested_fanout=3,
                current_inflight=7,
            )
        )

    async def log_audit(_session, **kwargs):
        audit_calls.append(kwargs)

    async def agent_context(_session):
        return SimpleNamespace(
            user=user,
            agent=SimpleNamespace(id="agent-a"),
        )

    async def resource_scope(_session, **_kwargs):
        return KnowledgeResourceScope({"instance-a": None})

    class PassingGovernor:
        async def check_rate(self, *_args):
            return None

    monkeypatch.setattr(
        polarrag,
        "get_session_factory",
        lambda: session_context,
    )
    monkeypatch.setattr(polarrag, "current_agent_user_context", agent_context)
    monkeypatch.setattr(
        polarrag,
        "current_polarrag_resource_scope",
        resource_scope,
    )
    monkeypatch.setattr(polarrag, "log_audit", log_audit)
    monkeypatch.setattr(
        polarrag,
        "get_polarrag_tool_governor",
        PassingGovernor,
    )

    result = await polarrag._execute_tool("kb_search", handler)

    assert result.isError is True
    assert json.loads(audit_calls[-1]["client_info"]) == {
        "polarrag_status": "POLARRAG_TOOL_LIMITED",
        "governance_reason": "INSTANCE_CONCURRENCY",
        "retry_after_seconds": 4,
        "requested_fanout": 3,
        "current_inflight": 7,
    }
    assert "requested_fanout" not in result.content[0].text
    assert "current_inflight" not in result.content[0].text
