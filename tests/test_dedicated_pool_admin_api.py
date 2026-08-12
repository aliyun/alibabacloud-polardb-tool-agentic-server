from __future__ import annotations

import base64
import os
from unittest.mock import AsyncMock

from server.config import TenantProvisioningConfig, get_config
from server.core.crypto import encrypt
from server.core.dedicated_pool_worker import DedicatedPoolWorker
from server.models import (
    Agent,
    AgentProvisioningBinding,
    AllocationMode,
    CredentialCapability,
    CredentialPurpose,
    DedicatedPool,
    DedicatedPoolMember,
    DedicatedPreparationStep,
    Instance,
    InstanceCredential,
    InstanceStatus,
    InstanceTopology,
)

pytest_plugins = ("tests._admin_api_fixtures",)


async def _revision(http, admin_headers, name="pool-template"):
    template = (
        await http.post(
            "/api/permission-templates",
            headers=admin_headers,
            json={"name": name},
        )
    ).json()
    return (
        await http.post(
            f"/api/permission-templates/{template['id']}/revisions",
            headers=admin_headers,
            json={"privileges": ["SELECT", "INSERT"], "grant_option": False},
        )
    ).json()


def _pool_body(revision_id):
    return {
        "name": "agent-dedicated",
        "target_size": 1,
        "max_total_members": 3,
        "max_member_purchases_per_hour": 2,
        "max_create_requests_per_agent_per_hour": 10,
        "max_delete_requests_per_agent_per_hour": 10,
        "purchase_profile_id": "agentic-dedicated-mysql",
        "purchase_profile_revision": 1,
        "storage_type": "essdpl1",
        "region_id": "cn-hangzhou",
        "zone_id": "cn-hangzhou-k",
        "vpc_id": "vpc-test",
        "vswitch_id": "vsw-test",
        "reclaim_policy": "sanitize_and_reuse",
        "lifecycle_admin_policy": "pas_managed",
        "permission_template_revision_id": revision_id,
        "delete_cooldown_duration_hours": 24,
        "available_health_check_interval_seconds": 300,
        "available_health_stale_after_seconds": 600,
    }


async def test_admin_creates_updates_and_drains_dedicated_pool(client):
    http, admin_headers, member_headers = client
    revision = await _revision(http, admin_headers)
    forbidden = await http.post(
        "/api/dedicated-pools",
        headers=member_headers,
        json=_pool_body(revision["id"]),
    )
    assert forbidden.status_code == 403

    created = await http.post(
        "/api/dedicated-pools",
        headers=admin_headers,
        json=_pool_body(revision["id"]),
    )
    assert created.status_code == 201
    payload = created.json()
    assert payload["target_size"] == 1
    assert payload["allocatable"] == 0
    assert payload["planning"] == 0
    assert payload["billable_total"] == 0
    assert payload["surplus"] == 0
    assert payload["supply_state"] == "not_started"
    assert payload["supply_current"] == 0
    assert payload["supply_target"] == 1
    assert "DEDICATED_WORKER_DISABLED" in payload["blocking_reasons"]
    assert "password" not in str(payload).lower()
    pool_id = payload["id"]

    invalid = await http.patch(
        f"/api/dedicated-pools/{pool_id}",
        headers=admin_headers,
        json={
            "expected_config_revision": payload["config_revision"],
            "available_health_stale_after_seconds": 599,
        },
    )
    assert invalid.status_code == 422

    updated = await http.patch(
        f"/api/dedicated-pools/{pool_id}",
        headers=admin_headers,
        json={
            "expected_config_revision": payload["config_revision"],
            "target_size": 2,
            "max_total_members": 4,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["config_revision"] == 2

    unsupported_storage = await http.patch(
        f"/api/dedicated-pools/{pool_id}",
        headers=admin_headers,
        json={
            "expected_config_revision": updated.json()["config_revision"],
            "storage_type": "ESSDAUTOPL",
        },
    )
    assert unsupported_storage.status_code == 422
    assert unsupported_storage.json()["detail"]["code"] == "INVALID_ARGUMENT"

    drained = await http.post(
        f"/api/dedicated-pools/{pool_id}/drain",
        headers=admin_headers,
    )
    assert drained.status_code == 200
    assert drained.json()["status"] == "draining"
    assert drained.json()["target_size"] == 0


async def test_admin_normalizes_pool_network_identifiers(client):
    http, admin_headers, _ = client
    revision = await _revision(http, admin_headers, "normalized-network")
    body = _pool_body(revision["id"])
    body.update(
        {
            "region_id": " cn-hangzhou ",
            "zone_id": " cn-hangzhou-k ",
            "vpc_id": " vpc-normalized ",
            "vswitch_id": " vsw-normalized ",
            "security_ip_list": " 10.0.0.0/24 ",
        }
    )

    created = await http.post(
        "/api/dedicated-pools",
        headers=admin_headers,
        json=body,
    )

    assert created.status_code == 201
    payload = created.json()
    assert payload["region_id"] == "cn-hangzhou"
    assert payload["zone_id"] == "cn-hangzhou-k"
    assert payload["vpc_id"] == "vpc-normalized"
    assert payload["vswitch_id"] == "vsw-normalized"
    assert payload["security_ip_list"] == "10.0.0.0/24"

    body["name"] = "blank-network"
    body["vpc_id"] = "   "
    blank = await http.post(
        "/api/dedicated-pools",
        headers=admin_headers,
        json=body,
    )
    assert blank.status_code == 422


async def test_admin_confirms_network_change_and_rejects_stale_revision(client):
    http, admin_headers, _ = client
    revision = await _revision(http, admin_headers, "editable-network")
    created = await http.post(
        "/api/dedicated-pools",
        headers=admin_headers,
        json=_pool_body(revision["id"]),
    )
    assert created.status_code == 201
    pool = created.json()
    pool_id = pool["id"]

    unconfirmed = await http.patch(
        f"/api/dedicated-pools/{pool_id}",
        headers=admin_headers,
        json={
            "expected_config_revision": pool["config_revision"],
            "vpc_id": " vpc-updated ",
        },
    )
    assert unconfirmed.status_code == 422
    assert unconfirmed.json()["detail"]["code"] == (
        "NETWORK_CHANGE_CONFIRMATION_REQUIRED"
    )

    updated = await http.patch(
        f"/api/dedicated-pools/{pool_id}",
        headers=admin_headers,
        json={
            "expected_config_revision": pool["config_revision"],
            "network_change_confirmed": True,
            "region_id": " cn-beijing ",
            "zone_id": " cn-beijing-k ",
            "vpc_id": " vpc-updated ",
            "vswitch_id": " vsw-updated ",
        },
    )
    assert updated.status_code == 200
    updated_payload = updated.json()
    assert updated_payload["config_revision"] == pool["config_revision"] + 1
    assert updated_payload["region_id"] == "cn-beijing"
    assert updated_payload["zone_id"] == "cn-beijing-k"
    assert updated_payload["vpc_id"] == "vpc-updated"
    assert updated_payload["vswitch_id"] == "vsw-updated"

    whitelist = await http.patch(
        f"/api/dedicated-pools/{pool_id}",
        headers=admin_headers,
        json={
            "expected_config_revision": updated_payload["config_revision"],
            "security_ip_list": " 10.1.0.0/24 ",
        },
    )
    assert whitelist.status_code == 200
    assert whitelist.json()["security_ip_list"] == "10.1.0.0/24"

    stale = await http.patch(
        f"/api/dedicated-pools/{pool_id}",
        headers=admin_headers,
        json={
            "expected_config_revision": pool["config_revision"],
            "network_change_confirmed": True,
            "vpc_id": "vpc-stale-write",
        },
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "POOL_CONFIGURATION_CONFLICT"

    current = await http.get(
        f"/api/dedicated-pools/{pool_id}", headers=admin_headers
    )
    assert current.status_code == 200
    assert current.json()["vpc_id"] == "vpc-updated"


async def test_admin_treats_normalized_legacy_network_as_unchanged(
    client, setup
):
    http, admin_headers, _ = client
    factory, _admin, _member = setup
    revision = await _revision(http, admin_headers, "legacy-whitespace")
    created = await http.post(
        "/api/dedicated-pools",
        headers=admin_headers,
        json=_pool_body(revision["id"]),
    )
    assert created.status_code == 201
    pool = created.json()

    async with factory() as session:
        stored = await session.get(DedicatedPool, pool["id"])
        assert stored is not None
        stored.vpc_id = " vpc-test "
        await session.commit()

    updated = await http.patch(
        f"/api/dedicated-pools/{pool['id']}",
        headers=admin_headers,
        json={
            "expected_config_revision": pool["config_revision"],
            "name": "normalized legacy pool",
            "vpc_id": "vpc-test",
        },
    )
    assert updated.status_code == 200
    assert updated.json()["vpc_id"] == "vpc-test"


async def test_admin_reads_pool_detail_with_safe_member_and_route_usage(
    client,
    setup,
):
    http, admin_headers, _ = client
    factory, admin, _member = setup
    revision = await _revision(http, admin_headers, "detail-template")
    created = await http.post(
        "/api/dedicated-pools",
        headers=admin_headers,
        json=_pool_body(revision["id"]),
    )
    assert created.status_code == 201
    pool_id = created.json()["id"]

    async with factory() as session:
        pool = await session.get(DedicatedPool, pool_id)
        assert pool is not None
        agent = Agent(name="detail-agent", created_by=admin.id)
        instance = Instance(
            cluster_id="pc-detail-member",
            name="detail-member",
            topology=InstanceTopology.SINGLE_TENANT,
            allocation_mode=AllocationMode.AUTO_PROVISIONED,
            status=InstanceStatus.ACTIVE,
        )
        diagnostic_instance = Instance(
            cluster_id="pc-diagnostic-member",
            name="diagnostic-member",
            topology=InstanceTopology.SINGLE_TENANT,
            allocation_mode=AllocationMode.AUTO_PROVISIONED,
            status=InstanceStatus.ACTIVE,
        )
        session.add_all([agent, instance, diagnostic_instance])
        await session.flush()
        assert pool.provisioning_backend is not None
        session.add_all(
            [
                AgentProvisioningBinding(
                    agent_id=agent.id,
                    backend_id=pool.provisioning_backend.id,
                    enabled=True,
                    routing_order=0,
                    created_by_user_id=admin.id,
                ),
                DedicatedPoolMember(
                    pool_id=pool.id,
                    instance_id=instance.id,
                    cloud_request_id="request-safe-1",
                    failure_reason="password=must-not-leak",
                ),
                DedicatedPoolMember(
                    pool_id=pool.id,
                    instance_id=diagnostic_instance.id,
                    cloud_request_id="request-diagnostic-1",
                    failure_reason=(
                        '{"code":"DEDICATED_PREWARM_INVALIDACCOUNTNAME_NOTFOUND",'
                        '"detail":"Account pas_lifecycle_demo is not exist!",'
                        '"occurred_at":"2026-08-11T14:19:19+00:00",'
                        '"operation":"CreateDatabase"}'
                    ),
                ),
            ]
        )
        await session.commit()
        agent_id = agent.id

    detail = await http.get(
        f"/api/dedicated-pools/{pool_id}", headers=admin_headers
    )

    assert detail.status_code == 200
    payload = detail.json()
    assert payload["route_usage"] == [
        {
            "agent_id": agent_id,
            "agent_name": "detail-agent",
            "binding_id": payload["route_usage"][0]["binding_id"],
            "enabled": True,
            "routing_order": 0,
            "role": "primary",
        }
    ]
    members = {member["cloud_request_id"]: member for member in payload["members"]}
    assert members["request-safe-1"]["failure_reason"] == (
        "DEDICATED_MEMBER_FAILURE"
    )
    assert members["request-safe-1"]["failure_detail"] is None
    diagnostic = members["request-diagnostic-1"]
    assert diagnostic["failure_reason"] == (
        "DEDICATED_PREWARM_INVALIDACCOUNTNAME_NOTFOUND"
    )
    assert diagnostic["failure_detail"] == (
        "Account pas_lifecycle_demo is not exist!"
    )
    assert diagnostic["failure_occurred_at"] == "2026-08-11T14:19:19Z"
    assert diagnostic["failure_operation"] == "CreateDatabase"
    assert "must-not-leak" not in str(payload)
    assert "purchase_token" not in payload["members"][0]


async def test_admin_reads_complete_dedicated_readiness(client):
    http, admin_headers, member_headers = client

    forbidden = await http.get(
        "/api/dedicated-pools/readiness", headers=member_headers
    )
    assert forbidden.status_code == 403

    response = await http.get(
        "/api/dedicated-pools/readiness", headers=admin_headers
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["worker"] == {
        "configured": False,
        "active_worker_count": 0,
        "last_heartbeat_at": None,
    }
    assert payload["purchase_profile"]["profile_id"] == (
        "agentic-dedicated-mysql"
    )
    assert payload["simulation_mode"] is False
    assert payload["preparation_mode"] == "full"
    assert payload["blocking_reasons"] == [
        "DEDICATED_WORKER_DISABLED",
        "ALIYUN_ACCESS_NOT_CONFIGURED",
        "PERMISSION_TEMPLATE_UNAVAILABLE",
    ]


async def test_admin_rejects_legacy_purchase_config_field(client):
    http, admin_headers, _ = client
    revision = await _revision(http, admin_headers, "legacy-json-template")
    body = _pool_body(revision["id"])
    body.pop("purchase_profile_id")
    body.pop("purchase_profile_revision")
    body.pop("storage_type")
    body["purchase_config"] = {"db_node_class": "ignored-value"}

    response = await http.post(
        "/api/dedicated-pools",
        headers=admin_headers,
        json=body,
    )

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "code": "PURCHASE_CONFIG_NOT_SUPPORTED",
        "field": "body.purchase_config",
        "message": "Raw purchase_config is not supported",
    }


async def test_admin_explicitly_upgrades_legacy_purchase_profile(
    client,
    setup,
):
    http, admin_headers, _ = client
    factory, _admin, _member = setup
    revision = await _revision(http, admin_headers, "upgrade-template")
    created = await http.post(
        "/api/dedicated-pools",
        headers=admin_headers,
        json=_pool_body(revision["id"]),
    )
    assert created.status_code == 201
    pool_id = created.json()["id"]

    async with factory() as session:
        pool = await session.get(DedicatedPool, pool_id)
        assert pool is not None
        pool.purchase_config_json = "{}"
        pool.purchase_profile_id = None
        pool.purchase_profile_revision = None
        pool.storage_type = None
        await session.commit()

    listed = await http.get("/api/dedicated-pools", headers=admin_headers)
    legacy = next(item for item in listed.json() if item["id"] == pool_id)
    assert legacy["purchase_profile_status"] == "upgrade_required"
    assert "purchase_config" not in legacy

    stale = await http.post(
        f"/api/dedicated-pools/{pool_id}/purchase-profile/upgrade",
        headers=admin_headers,
        json={"expected_config_revision": 99},
    )
    assert stale.status_code == 409

    upgraded = await http.post(
        f"/api/dedicated-pools/{pool_id}/purchase-profile/upgrade",
        headers=admin_headers,
        json={"expected_config_revision": legacy["config_revision"]},
    )
    assert upgraded.status_code == 200
    assert upgraded.json()["purchase_profile_status"] == "valid"
    assert upgraded.json()["purchase_profile_id"] == "agentic-dedicated-mysql"
    assert upgraded.json()["purchase_profile_revision"] == 1
    assert upgraded.json()["storage_type"] == "essdpl1"
    assert upgraded.json()["config_revision"] == legacy["config_revision"] + 1


async def test_external_member_requires_admin_provided_lifecycle_credential(
    client, setup, monkeypatch
):
    monkeypatch.setenv(
        "PAS_ENCRYPTION_KEY",
        base64.b64encode(os.urandom(32)).decode("ascii"),
    )
    http, admin_headers, _ = client
    factory, admin, _member = setup
    revision = await _revision(http, admin_headers, "external-template")
    body = _pool_body(revision["id"])
    body["name"] = "external-pool"
    body["target_size"] = 0
    body["lifecycle_admin_policy"] = "admin_provided"
    pool = (
        await http.post(
            "/api/dedicated-pools", headers=admin_headers, json=body
        )
    ).json()

    async with factory() as session:
        instance = Instance(
            cluster_id="pc-external-dedicated",
            name="External Dedicated",
            topology=InstanceTopology.SINGLE_TENANT,
            allocation_mode=AllocationMode.REGISTERED,
            status=InstanceStatus.ACTIVE,
            host="external.internal",
            port=3306,
        )
        session.add(instance)
        await session.flush()
        credential = InstanceCredential(
            instance_id=instance.id,
            name="lifecycle-admin",
            purpose=CredentialPurpose.PROVISIONING_ADMIN,
            capability=CredentialCapability.ADMIN,
            username_ciphertext=encrypt("admin"),
            password_ciphertext=encrypt("secret"),
            created_by_user_id=admin.id,
        )
        session.add(credential)
        await session.commit()
        instance_id, credential_id = instance.id, credential.id

    missing = await http.post(
        f"/api/dedicated-pools/{pool['id']}/members",
        headers=admin_headers,
        json={"instance_id": instance_id},
    )
    assert missing.status_code == 422
    added = await http.post(
        f"/api/dedicated-pools/{pool['id']}/members",
        headers=admin_headers,
        json={
            "instance_id": instance_id,
            "lifecycle_credential_id": credential_id,
        },
    )
    assert added.status_code == 201
    assert added.json()["status"] == "replenishing"
    assert added.json()["actions"]["retry"] is True
    assert "ciphertext" not in str(added.json()).lower()
    assert "secret" not in str(added.json()).lower()
    member_id = added.json()["id"]

    retried = await http.post(
        f"/api/dedicated-pools/{pool['id']}/members/"
        f"{member_id}/actions/retry",
        headers=admin_headers,
    )
    assert retried.status_code == 200
    assert retried.json()["status"] == "replenishing"

    async with factory() as session:
        member = await session.get(DedicatedPoolMember, member_id)
        assert member is not None
        member.preparation_step = DedicatedPreparationStep.OPENAPI_READY
        await session.commit()
    get_config().polardb.tenant_provisioning.dedicated_pool_preparation_mode = (
        "openapi_only"
    )
    paused = await http.get(
        f"/api/dedicated-pools/{pool['id']}", headers=admin_headers
    )
    assert paused.status_code == 200
    assert paused.json()["members"][0]["actions"]["retry"] is False
    blocked_retry = await http.post(
        f"/api/dedicated-pools/{pool['id']}/members/"
        f"{member_id}/actions/retry",
        headers=admin_headers,
    )
    assert blocked_retry.status_code == 409
    get_config().polardb.tenant_provisioning.dedicated_pool_preparation_mode = (
        "full"
    )

    overridden = await http.patch(
        f"/api/dedicated-pools/{pool['id']}/members/{member_id}",
        headers=admin_headers,
        json={"delete_cooldown_duration_hours": 12},
    )
    assert overridden.status_code == 200
    assert overridden.json()["delete_cooldown_source"] == "member"
    inherited = await http.patch(
        f"/api/dedicated-pools/{pool['id']}/members/{member_id}",
        headers=admin_headers,
        json={"delete_cooldown_duration_hours": None},
    )
    assert inherited.status_code == 200
    assert inherited.json()["delete_cooldown_source"] == "pool"

    for action, expected in (
        ("quarantine", "quarantined"),
        ("retry", "replenishing"),
        ("quarantine", "quarantined"),
        ("destroy", "deleting"),
    ):
        changed = await http.post(
            f"/api/dedicated-pools/{pool['id']}/members/"
            f"{member_id}/actions/{action}",
            headers=admin_headers,
        )
        assert changed.status_code == 200
        assert changed.json()["status"] == expected

    provisioner = AsyncMock()
    worker = DedicatedPoolWorker(
        factory,
        TenantProvisioningConfig(
            worker_poll_interval_seconds=1,
            worker_claim_ttl_seconds=10,
            worker_claim_renew_seconds=1,
        ),
        provisioner,
        AsyncMock(),
        worker_id="admin-destroy-worker",
    )
    assert await worker.run_once() is True
    provisioner.delete_cluster.assert_awaited_once_with(
        "pc-external-dedicated"
    )
