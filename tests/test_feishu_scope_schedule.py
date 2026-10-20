from datetime import UTC, datetime, timedelta

from server.config import EnterpriseIdentitySyncConfig
from server.enterprise_identity.providers import FeishuDirectorySyncAdapter
from server.models import EnterpriseIdentitySource, EnterpriseIdentitySourceStatus, FeishuEventSubscriptionStatus


def test_subscription_error_does_not_delay_due_group_and_scope_refresh():
    now = datetime.now(UTC)
    source = EnterpriseIdentitySource(
        status=EnterpriseIdentitySourceStatus.ACTIVE,
        event_listener_status=FeishuEventSubscriptionStatus.ERROR,
        last_synced_at=now - timedelta(hours=2),
        last_group_synced_at=now - timedelta(hours=2),
        event_unavailable_since=now - timedelta(minutes=10),
    )
    settings = EnterpriseIdentitySyncConfig()
    adapter = FeishuDirectorySyncAdapter()
    request = adapter.due_sync(source, now, settings, 1800)
    assert request is not None and request.kind == "groups"
    deadlines = adapter.next_due_at(source, now, settings, 1800)
    assert min(deadlines) <= now
    assert max(deadlines) > now


def test_group_and_scope_interval_legacy_value_survives_and_schema_uses_new_name():
    settings = EnterpriseIdentitySyncConfig.model_validate({"feishu_group_interval_seconds": 7200})
    assert settings.feishu_group_and_knowledge_scope_interval_seconds == 7200
    assert settings.feishu_group_interval_seconds == 7200
    schema = EnterpriseIdentitySyncConfig.model_json_schema()["properties"]
    assert "feishu_group_and_knowledge_scope_interval_seconds" in schema
    assert "feishu_group_interval_seconds" not in schema



def test_group_failure_uses_group_interval_not_full_fallback():
    from server.enterprise_identity.sync import _retry_delay_seconds
    import httpx
    now = datetime.now(UTC)
    source = EnterpriseIdentitySource(status=EnterpriseIdentitySourceStatus.ACTIVE,
        last_synced_at=now, last_group_synced_at=now - timedelta(minutes=11))
    settings = EnterpriseIdentitySyncConfig(feishu_group_and_knowledge_scope_interval_seconds=600,
        feishu_fallback_after_seconds=86400)
    request = FeishuDirectorySyncAdapter().due_sync(source, now, settings, 1800)
    assert request.kind == "groups" and request.interval_seconds == 600
    assert _retry_delay_seconds(httpx.ReadTimeout("timeout"), 1, fallback_seconds=request.interval_seconds) == 600
