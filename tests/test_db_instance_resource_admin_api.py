from __future__ import annotations

from server.models import (
    Agent,
    AllocationMode,
    DBInstanceResource,
    DBInstanceStatus,
    DedicatedMemberStatus,
    DedicatedPool,
    DedicatedPoolMember,
    Instance,
    InstanceStatus,
    InstanceTopology,
    PermissionTemplate,
    PermissionTemplateRevision,
    ProvisioningBackend,
    ProvisioningBackendType,
    ProvisioningMode,
    ReadinessStatus,
)

pytest_plugins = ("tests._admin_api_fixtures",)


async def test_admin_lists_sanitized_resource_and_requests_restore(client, setup):
    http, admin_headers, member_headers = client
    factory, _admin, _member = setup
    async with factory() as session:
        agent = Agent(name="resource-admin-agent")
        template = PermissionTemplate(name="resource-admin-template")
        revision = PermissionTemplateRevision(
            template=template,
            revision=1,
            privileges_json='["SELECT"]',
        )
        pool = DedicatedPool(
            name="resource-admin-pool",
            target_size=0,
            max_total_members=2,
            max_member_purchases_per_hour=2,
            max_create_requests_per_agent_per_hour=10,
            max_delete_requests_per_agent_per_hour=10,
            purchase_config_json="{}",
            region_id="cn-hangzhou",
            vpc_id="vpc-admin",
            vswitch_id="vsw-admin",
            permission_template_revision=revision,
        )
        backend = ProvisioningBackend(
            backend_type=ProvisioningBackendType.DEDICATED_POOL,
            dedicated_pool=pool,
            permission_template_revision=revision,
            max_active_resources=10,
        )
        instance = Instance(
            cluster_id="pc-resource-admin",
            name="Resource admin member",
            topology=InstanceTopology.SINGLE_TENANT,
            allocation_mode=AllocationMode.POOLED,
            status=InstanceStatus.ACTIVE,
            host="resource-admin.internal",
            port=3306,
        )
        session.add_all([agent, backend, instance])
        await session.flush()
        resource = DBInstanceResource(
            owner_agent_id=agent.id,
            backend_id=backend.id,
            client_token="admin-resource",
            request_fingerprint="a" * 64,
            provisioning_mode=ProvisioningMode.DEDICATED,
            allocated_instance_id=instance.id,
            status=DBInstanceStatus.COOLING_DOWN,
            database_name="agentic",
            effective_delete_cooldown_duration_hours=24,
        )
        session.add(resource)
        await session.flush()
        session.add(
            DedicatedPoolMember(
                pool=pool,
                instance=instance,
                allocated_resource=resource,
                status=DedicatedMemberStatus.COOLING_DOWN,
                readiness_status=ReadinessStatus.FRESH,
            )
        )
        await session.commit()
        resource_id = resource.id

    forbidden = await http.post(
        f"/api/db-instance-resources/{resource_id}/restore",
        headers=member_headers,
    )
    assert forbidden.status_code == 403
    listed = await http.get(
        "/api/db-instance-resources", headers=admin_headers
    )
    assert listed.status_code == 200
    row = next(item for item in listed.json() if item["id"] == resource_id)
    assert row["status"] == "cooling_down"
    assert row["actions"]["restore"] is True
    assert "password" not in str(row).lower()

    restored = await http.post(
        f"/api/db-instance-resources/{resource_id}/restore",
        headers=admin_headers,
    )
    assert restored.status_code == 202
    assert restored.json()["status"] == "restoring"
    repeated = await http.post(
        f"/api/db-instance-resources/{resource_id}/restore",
        headers=admin_headers,
    )
    assert repeated.status_code == 409
