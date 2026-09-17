from server.models import (
    Agent,
    Instance,
    AllocationMode,
    InstanceStatus,
    UserInstanceBinding,
    BindingOrigin,
    Permission,
)

pytest_plugins = ("tests._admin_api_fixtures",)


async def test_console_scopes_pagination_and_disable(client, setup):
    http, admin_headers, member_headers = client
    factory, admin, member = setup
    async with factory() as session:
        instance = Instance(
            cluster_id="console-db",
            name="Reports",
            allocation_mode=AllocationMode.REGISTERED,
            status=InstanceStatus.ACTIVE,
        )
        agent = Agent(name="Reports bot", created_by=admin.id)
        session.add_all([instance, agent])
        await session.flush()
        binding = UserInstanceBinding(
            user_id=member.id, instance_id=instance.id, origin=BindingOrigin.SYSTEM, permission=Permission.READONLY
        )
        session.add(binding)
        await session.commit()
        iid, aid = instance.id, agent.id
    for path in ["/access/resources", "/access/accounts", "/access/grants"]:
        assert (await http.get("/api" + path, headers=member_headers)).status_code == 403
    catalog = (await http.get("/api/access/resources", headers=admin_headers)).json()
    assert catalog["total"] == 1
    assert catalog["items"][0]["kind"] == "database"
    accounts = (await http.get("/api/access/accounts", headers=admin_headers, params={"kind": "service"})).json()
    assert [x["id"] for x in accounts["items"]] == [aid]
    params = {"account_kind": "personal", "account_id": member.id}
    grants = (await http.get("/api/access/grants", headers=admin_headers, params=params)).json()
    assert grants["total"] == 1
    assert grants["items"][0]["resource_id"] == iid
    assert grants["items"][0]["source"] == "system"
    assert "password" not in str(grants)
    assert (await http.get("/api/access/grants", headers=admin_headers, params={**params, "offset": 1})).json()[
        "items"
    ] == []
    by_resource = (await http.get("/api/access/grants", headers=admin_headers, params={"resource_id": iid})).json()
    assert by_resource["items"] == grants["items"]
    path = f"/api/access/accounts/personal/{member.id}/resources/{iid}/disable"
    assert (await http.post(path, headers=member_headers)).status_code == 403
    assert (await http.post(path, headers=admin_headers)).status_code == 204
    assert (await http.post(path, headers=admin_headers)).status_code == 204
    assert not (await http.get("/api/access/grants", headers=admin_headers, params=params)).json()["items"][0][
        "enabled"
    ]


async def test_service_and_inherited_grants_remain_separate(client, setup):
    from server.models import (
        AgentInstanceBinding,
        InstanceCredential,
        CredentialPurpose,
        CredentialCapability,
        Department,
        DepartmentInstanceBinding,
        UserDepartment,
    )

    http, admin_headers, _ = client
    factory, admin, member = setup
    async with factory() as session:
        instance = Instance(
            cluster_id="shared", name="Shared", allocation_mode=AllocationMode.REGISTERED, status=InstanceStatus.ACTIVE
        )
        agent = Agent(name="Bot", created_by=member.id)
        department = Department(name="Reports")
        session.add_all([instance, agent, department])
        await session.flush()
        credential = InstanceCredential(
            instance_id=instance.id,
            name="db-user",
            purpose=CredentialPurpose.DIRECT_ACCESS,
            capability=CredentialCapability.READONLY,
            username_ciphertext="hidden",
            password_ciphertext="hidden",
        )
        session.add(credential)
        await session.flush()
        session.add_all(
            [
                AgentInstanceBinding(
                    agent_id=agent.id,
                    instance_id=instance.id,
                    credential_id=credential.id,
                    permission=Permission.READONLY,
                    created_by_user_id=admin.id,
                ),
                DepartmentInstanceBinding(
                    department_id=department.id, instance_id=instance.id, default_permission=Permission.READONLY
                ),
                UserDepartment(user_id=member.id, department_id=department.id),
            ]
        )
        await session.commit()
        iid, aid = instance.id, agent.id
    catalog = (await http.get("/api/access/grants", headers=admin_headers, params={"resource_id": iid})).json()
    assert catalog["total"] == 2
    assert {x["source"] for x in catalog["items"]} == {"admin", "department"}
    personal = (
        await http.get(
            "/api/access/grants", headers=admin_headers, params={"account_kind": "personal", "account_id": member.id}
        )
    ).json()
    assert [x["account_kind"] for x in personal["items"]] == ["group"]
    assert (
        await http.post(f"/api/access/accounts/service/{aid}/resources/{iid}/disable", headers=admin_headers)
    ).status_code == 204
    service = (
        await http.get(
            "/api/access/grants", headers=admin_headers, params={"account_kind": "service", "account_id": aid}
        )
    ).json()
    assert service["items"][0]["enabled"] is False


async def test_required_audit_failure_rolls_back_disable(client, setup, monkeypatch):
    http, admin_headers, _ = client
    factory, admin, member = setup
    async with factory() as session:
        instance = Instance(
            cluster_id="audit", name="Audit", allocation_mode=AllocationMode.REGISTERED, status=InstanceStatus.ACTIVE
        )
        session.add(instance)
        await session.flush()
        binding = UserInstanceBinding(user_id=member.id, instance_id=instance.id, origin=BindingOrigin.SYSTEM)
        session.add(binding)
        await session.commit()
        iid, bid = instance.id, binding.id

    async def fail(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr("server.api.access_console.log_audit", fail)
    response = await http.post(
        f"/api/access/accounts/personal/{member.id}/resources/{iid}/disable", headers=admin_headers
    )
    assert response.status_code == 503
    async with factory() as session:
        assert (await session.get(UserInstanceBinding, bid)).enabled


async def test_shared_sql_preset_preserves_other_capabilities_and_validates_credential(client, setup):
    from server.models import InstanceCredential, CredentialPurpose, CredentialCapability, BindingCapability
    from server.core import admin_binding_service

    http, admin_headers, member_headers = client
    factory, admin, member = setup
    async with factory() as session:
        instance = Instance(
            cluster_id="preset", name="Preset", allocation_mode=AllocationMode.REGISTERED, status=InstanceStatus.ACTIVE
        )
        other = Instance(
            cluster_id="other", name="Other", allocation_mode=AllocationMode.REGISTERED, status=InstanceStatus.ACTIVE
        )
        agent = Agent(name="Preset bot", created_by=member.id)
        session.add_all([instance, other, agent])
        await session.flush()
        creds = [
            InstanceCredential(
                instance_id=i.id,
                name="Direct",
                purpose=CredentialPurpose.DIRECT_ACCESS,
                capability=CredentialCapability.READWRITE,
                username_ciphertext="hidden",
                password_ciphertext="hidden",
            )
            for i in [instance, other]
        ]
        session.add_all(creds)
        await session.flush()
        await admin_binding_service.update_user_instance_access(
            session,
            user_id=member.id,
            instance_id=instance.id,
            credential_id=creds[0].id,
            permission=Permission.READWRITE,
            capabilities={
                BindingCapability.DB_INSTANCE_CREDENTIALS_READ,
                BindingCapability.SQL_READ,
                BindingCapability.SQL_WRITE,
            },
            enabled=True,
        )
        await session.commit()
        iid, aid, cid, bad = instance.id, agent.id, creds[0].id, creds[1].id
    for kind, account in [("personal", member.id), ("service", aid)]:
        path = f"/api/access/accounts/{kind}/{account}/resources/{iid}"
        body = {"credential_id": cid, "permission": "readonly"}
        assert (await http.put(path, headers=member_headers, json=body)).status_code == 403
        assert (await http.put(path, headers=admin_headers, json={**body, "credential_id": bad})).status_code == 422
        assert (await http.put(path, headers=admin_headers, json=body)).status_code == 200
        assert (await http.put(path, headers=admin_headers, json=body)).status_code == 200
    response = await http.get(f"/api/users/{member.id}/instance-access/{iid}", headers=admin_headers)
    assert "db_instance:credentials:read" in response.json()["capabilities"]
    assert "sql:write" not in response.json()["capabilities"]
    assert response.json()["permission"] == "readonly"
