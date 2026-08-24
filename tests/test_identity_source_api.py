from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import event, select

from server.core.crypto import decrypt
from server.models import (
    EnterpriseDirectoryMembershipType,
    EnterpriseDirectoryPrincipalType,
    EnterpriseDirectoryGroup,
    EnterpriseDirectoryUser,
    EnterpriseIdentitySource,
    EnterpriseIdentitySourceStatus,
    FeishuTenantVerificationState,
    FeishuUserLoginState,
    IdentitySourceProvider,
    PolarRAGInstance,
    PolarRAGInstanceStatus,
    PolarRAGSpace,
    UserExternalIdentity,
)
from server.enterprise_identity.service import (
    identity_provider_key,
    upsert_directory_group,
    upsert_directory_membership,
    upsert_external_user,
)

pytest_plugins = ("tests._admin_api_fixtures",)


async def test_identity_source_directory_paginates_users_and_groups(client, setup) -> None:
    http, admin_headers, _member_headers = client
    factory, _admin, _member = setup
    created = await http.post(
        "/api/identity-sources",
        json={
            "name": "Large directory",
            "provider": "sharepoint",
            "tenant_id": "tenant-large-directory",
            "client_id": "client-large-directory",
            "client_secret": "secret-large-directory",
        },
        headers=admin_headers,
    )
    source_id = created.json()["id"]
    async with factory() as session:
        session.add_all(
            [
                EnterpriseDirectoryUser(
                    identity_source_id=source_id,
                    external_user_id=f"user-{index:03d}",
                    display_name=f"User {index:03d}",
                )
                for index in range(205)
            ]
        )
        session.add_all(
            [
                EnterpriseDirectoryGroup(
                    identity_source_id=source_id,
                    external_group_id=f"group-{index:03d}",
                    display_name=f"Group {index:03d}",
                    principal_type=EnterpriseDirectoryPrincipalType.GROUP,
                )
                for index in range(205)
            ]
        )
        await session.commit()

    users = await http.get(
        f"/api/identity-sources/{source_id}/directory",
        params={"entry_type": "users", "offset": 200, "limit": 20},
        headers=admin_headers,
    )
    assert users.status_code == 200
    assert users.json()["total"] == 205
    assert [item["external_user_id"] for item in users.json()["users"]] == [
        "user-200",
        "user-201",
        "user-202",
        "user-203",
        "user-204",
    ]

    groups = await http.get(
        f"/api/identity-sources/{source_id}/directory",
        params={"entry_type": "groups", "offset": 200, "limit": 20},
        headers=admin_headers,
    )
    assert groups.status_code == 200
    assert groups.json()["total"] == 205
    assert [item["external_group_id"] for item in groups.json()["groups"]] == [
        "group-200",
        "group-201",
        "group-202",
        "group-203",
        "group-204",
    ]


async def test_identity_source_directory_skips_identity_lookup_without_users(
    client, setup
) -> None:
    http, admin_headers, _member_headers = client
    factory, _admin, _member = setup
    created = await http.post(
        "/api/identity-sources",
        json={
            "name": "Empty directory",
            "provider": "sharepoint",
            "tenant_id": "tenant-empty-directory",
            "client_id": "client-empty-directory",
            "client_secret": "secret-empty-directory",
        },
        headers=admin_headers,
    )
    source_id = created.json()["id"]
    statements: list[str] = []

    def record_statement(
        _connection, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        if "user_external_identities" in statement:
            statements.append(statement)

    engine = factory.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        response = await http.get(
            f"/api/identity-sources/{source_id}/directory",
            params={"entry_type": "groups"},
            headers=admin_headers,
        )
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)

    assert response.status_code == 200
    assert response.json()["groups"] == []
    assert statements == []


async def test_identity_source_candidates_use_trusted_internal_ids(
    client, setup
) -> None:
    http, admin_headers, _member_headers = client
    factory, admin, member = setup
    async with factory() as session:
        source = EnterpriseIdentitySource.create(
            name="Trusted candidate directory",
            provider=IdentitySourceProvider.SHAREPOINT,
            tenant_id="tenant-trusted-candidates",
        )
        source.status = EnterpriseIdentitySourceStatus.ACTIVE
        source.last_synced_at = datetime.now(UTC)
        session.add(source)
        await session.flush()
        mapped_directory_user = EnterpriseDirectoryUser(
            identity_source_id=source.id,
            external_user_id="mapped-user",
            display_name="Mapped user",
        )
        group = EnterpriseDirectoryGroup(
            identity_source_id=source.id,
            external_group_id="trusted-group",
            display_name="Trusted group",
            principal_type=EnterpriseDirectoryPrincipalType.GROUP,
        )
        session.add_all([mapped_directory_user, group])
        await session.flush()
        session.add(
            UserExternalIdentity(
                user_id=member.id,
                identity_provider=identity_provider_key(source),
                external_subject=mapped_directory_user.external_user_id,
            )
        )
        auto_user = await upsert_external_user(
            session,
            source,
            external_user_id="auto-user",
            display_name="Auto user",
            email=None,
        )
        instance = PolarRAGInstance(
            name="Trusted candidate PolarRAG",
            scheme="https",
            host="trusted-candidate.example.test",
            port=443,
            username_ciphertext="encrypted",
            password_ciphertext="encrypted",
            status=PolarRAGInstanceStatus.ACTIVE,
            created_by=admin.id,
        )
        session.add(instance)
        await session.flush()
        space = PolarRAGSpace(
            polarrag_instance_id=instance.id,
            space_id="trusted-space",
            name="Trusted Space",
            identity_domain="trusted-domain",
            enabled=True,
        )
        session.add(space)
        await session.commit()
        source_id = source.id
        mapped_directory_user_id = mapped_directory_user.id
        group_id = group.id
        instance_id = instance.id
        space_id = space.knowledge_space_id

    directory = await http.get(
        f"/api/identity-sources/{source_id}/directory",
        headers=admin_headers,
    )

    assert directory.status_code == 200
    users_by_external_id = {
        item["external_user_id"]: item for item in directory.json()["users"]
    }
    assert users_by_external_id["mapped-user"]["id"] == mapped_directory_user_id
    assert users_by_external_id["mapped-user"]["pas_user_id"] == member.id
    assert users_by_external_id["auto-user"]["pas_user_id"] == auto_user.id
    assert directory.json()["groups"][0]["id"] == group_id
    assert "client_secret" not in directory.text
    assert "acl_context" not in directory.text

    spaces = await http.get(
        "/api/identity-sources/spaces",
        headers=admin_headers,
    )
    assert spaces.status_code == 200
    assert spaces.json()["items"] == [
        {
            "knowledge_space_id": space_id,
            "polarrag_instance_id": instance_id,
            "name": "Trusted Space",
            "identity_domain": "trusted-domain",
        }
    ]


async def test_admin_maps_a_pas_user_to_a_synced_enterprise_identity(client, setup) -> None:
    http, admin_headers, _member_headers = client
    factory, _admin, member = setup
    created = await http.post(
        "/api/identity-sources",
        json={
            "name": "Feishu directory",
            "provider": "feishu",
            "app_id": "cli_directory",
            "app_secret": "secret-directory",
        },
        headers=admin_headers,
    )
    source_id = created.json()["id"]
    async with factory() as session:
        source = await session.get(EnterpriseIdentitySource, source_id)
        assert source is not None
        source.tenant_id = "tenant-directory"
        source.status = "active"
        synced_alice = await upsert_external_user(
            session,
            source,
            external_user_id="ou_alice",
            display_name="Alice",
            email="alice@example.com",
        )
        await upsert_external_user(
            session,
            source,
            external_user_id="ou_bob",
            display_name="Bob",
            email="bob@example.com",
        )
        department = await upsert_directory_group(
            session,
            source,
            external_group_id="od_engineering",
            display_name="Engineering",
            principal_type=EnterpriseDirectoryPrincipalType.DEPARTMENT,
        )
        await upsert_directory_membership(
            session,
            source,
            external_group_id=department.external_group_id,
            member_type=EnterpriseDirectoryMembershipType.USER,
            external_member_id="ou_alice",
        )
        group = await upsert_directory_group(
            session,
            source,
            external_group_id="oc_security",
            display_name="Security",
            principal_type=EnterpriseDirectoryPrincipalType.GROUP,
        )
        await upsert_directory_membership(
            session,
            source,
            external_group_id=group.external_group_id,
            member_type=EnterpriseDirectoryMembershipType.USER,
            external_member_id="ou_alice",
        )
        await session.commit()

    mapped = await http.post(
        f"/api/identity-sources/users/{member.id}/identities",
        json={"identity_source_id": source_id, "external_user_id": "ou_alice"},
        headers=admin_headers,
    )

    assert mapped.status_code == 201
    payload = mapped.json()
    assert payload["mapping_mode"] == "pas_managed"
    assert payload["source_name"] == "Feishu directory"
    assert payload["external_user_id"] == "ou_alice"
    assert payload["native_principal_id"] == member.external_id
    assert payload["principals"] == [
        {"provider": "feishu", "type": "department", "id": "od_engineering"},
        {"provider": "feishu", "type": "group", "id": "oc_security"},
        {"provider": "feishu", "type": "user", "id": "ou_alice"},
    ]

    users = await http.get("/api/users", headers=admin_headers)
    listed_member = next(item for item in users.json()["items"] if item["id"] == member.id)
    assert listed_member["auth_provider"] == "builtin"
    assert listed_member["identity_sources"] == []
    listed_synced_alice = next(item for item in users.json()["items"] if item["id"] == synced_alice.id)
    assert listed_synced_alice["enterprise_identities"] == [
        {
            "id": source_id,
            "name": "Feishu directory",
            "provider": "feishu",
            "external_user_id": "ou_alice",
            "departments": [{"id": "od_engineering", "name": "Engineering"}],
            "groups": [{"id": "oc_security", "name": "Security"}],
        }
    ]

    listed = await http.get(
        f"/api/identity-sources/users/{member.id}/identities",
        headers=admin_headers,
    )
    assert listed.status_code == 200
    assert listed.json()["items"] == [payload]

    updated = await http.put(
        f"/api/identity-sources/users/{member.id}/identities/{payload['id']}",
        json={"identity_source_id": source_id, "external_user_id": "ou_bob"},
        headers=admin_headers,
    )
    assert updated.status_code == 200
    assert updated.json()["external_user_id"] == "ou_bob"
    assert updated.json()["mapping_mode"] == "pas_managed"

    deleted = await http.delete(
        f"/api/identity-sources/users/{member.id}/identities/{updated.json()['id']}",
        headers=admin_headers,
    )
    assert deleted.status_code == 204
    listed_after_delete = await http.get(
        f"/api/identity-sources/users/{member.id}/identities",
        headers=admin_headers,
    )
    assert listed_after_delete.json()["items"] == []

    users = await http.get("/api/users", headers=admin_headers)
    listed_member = next(item for item in users.json()["items"] if item["id"] == member.id)
    assert listed_member["identity_sources"] == []
    async with factory() as session:
        alice_identity = (
            await session.execute(
                select(UserExternalIdentity).where(UserExternalIdentity.external_subject == "ou_alice")
            )
        ).scalar_one()
    listed_alice = next(item for item in users.json()["items"] if item["id"] == alice_identity.user_id)
    assert listed_alice["enterprise_identities"] == [
        {
            "id": source_id,
            "name": "Feishu directory",
            "provider": "feishu",
            "external_user_id": "ou_alice",
            "departments": [{"id": "od_engineering", "name": "Engineering"}],
            "groups": [{"id": "oc_security", "name": "Security"}],
        }
    ]


async def test_admin_maps_a_pas_user_to_a_synced_sharepoint_identity(client, setup) -> None:
    http, admin_headers, _member_headers = client
    factory, _admin, member = setup
    created = await http.post(
        "/api/identity-sources",
        json={
            "name": "SharePoint directory",
            "provider": "sharepoint",
            "tenant_id": "tenant-directory",
            "client_id": "client-directory",
            "client_secret": "secret-directory",
        },
        headers=admin_headers,
    )
    source_id = created.json()["id"]
    async with factory() as session:
        source = await session.get(EnterpriseIdentitySource, source_id)
        assert source is not None
        source.status = EnterpriseIdentitySourceStatus.ACTIVE
        await upsert_external_user(
            session,
            source,
            external_user_id="entra-alice",
            display_name="Alice",
            email="alice@example.com",
        )
        await session.commit()

    mapped = await http.post(
        f"/api/identity-sources/users/{member.id}/identities",
        json={"identity_source_id": source_id, "external_user_id": "entra-alice"},
        headers=admin_headers,
    )

    assert mapped.status_code == 201
    assert mapped.json()["source_name"] == "SharePoint directory"
    assert mapped.json()["principals"] == [{"provider": "sharepoint", "type": "user", "id": "entra-alice"}]


async def test_admin_creates_feishu_source_without_manual_tenant_key(client, setup):
    http, admin_headers, _member_headers = client

    created = await http.post(
        "/api/identity-sources",
        json={
            "name": "Feishu pending verification",
            "provider": "feishu",
            "app_id": "cli_test",
            "app_secret": "secret-value",
        },
        headers=admin_headers,
    )

    assert created.status_code == 201
    assert created.json()["tenant_id"] is None
    assert created.json()["status"] == "pending_tenant_verification"


async def test_admin_updates_feishu_source_and_must_reverify_tenant(client, setup) -> None:
    http, admin_headers, _member_headers = client
    factory, _admin, _member = setup
    created = await http.post(
        "/api/identity-sources",
        json={
            "name": "Feishu before update",
            "provider": "feishu",
            "app_id": "cli_before",
            "app_secret": "secret-before",
        },
        headers=admin_headers,
    )
    source_id = created.json()["id"]
    async with factory() as session:
        source = await session.get(EnterpriseIdentitySource, source_id)
        assert source is not None
        source.tenant_id = "tenant-before"
        source.status = "active"
        await session.commit()

    updated = await http.put(
        f"/api/identity-sources/{source_id}",
        json={
            "name": "Feishu after update",
            "app_id": "cli_after",
            "app_secret": "secret-after",
        },
        headers=admin_headers,
    )

    assert updated.status_code == 200
    assert updated.json()["name"] == "Feishu after update"
    assert updated.json()["tenant_id"] is None
    assert updated.json()["status"] == "pending_tenant_verification"
    async with factory() as session:
        source = await session.get(EnterpriseIdentitySource, source_id)
        assert source is not None
        assert json.loads(decrypt(source.config_ciphertext or "")) == {
            "app_id": "cli_after",
            "app_secret": "secret-after",
        }


async def test_admin_deletes_identity_source_and_its_directory_data(client, setup) -> None:
    http, admin_headers, _member_headers = client
    factory, _admin, _member = setup
    created = await http.post(
        "/api/identity-sources",
        json={
            "name": "Feishu to delete",
            "provider": "feishu",
            "app_id": "cli_test",
            "app_secret": "secret-value",
        },
        headers=admin_headers,
    )
    source_id = created.json()["id"]
    async with factory() as session:
        source = await session.get(EnterpriseIdentitySource, source_id)
        assert source is not None
        source.tenant_id = "tenant-delete"
        source.status = "active"
        await session.commit()

    deleted = await http.delete(f"/api/identity-sources/{source_id}", headers=admin_headers)

    assert deleted.status_code == 204
    async with factory() as session:
        assert await session.get(EnterpriseIdentitySource, source_id) is None


async def test_admin_starts_feishu_tenant_verification(client, setup, monkeypatch):
    http, admin_headers, _member_headers = client
    factory, _admin, _member = setup
    config = SimpleNamespace(server=SimpleNamespace(public_base_url="https://pas.example.test"))
    monkeypatch.setattr("server.api.identity_sources.get_config", lambda: config)
    monkeypatch.setattr("server.auth.router.get_config", lambda: config)
    created = await http.post(
        "/api/identity-sources",
        json={
            "name": "Feishu verification",
            "provider": "feishu",
            "app_id": "cli_test",
            "app_secret": "secret-value",
        },
        headers=admin_headers,
    )

    started = await http.post(
        f"/api/identity-sources/{created.json()['id']}/feishu-verification",
        headers=admin_headers,
    )

    assert started.status_code == 200
    authorization_url = started.json()["authorization_url"]
    parsed = urlparse(authorization_url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "accounts.feishu.cn"
    assert parsed.path == "/open-apis/authen/v1/authorize"
    params = parse_qs(parsed.query)
    assert params["client_id"] == ["cli_test"]
    assert params["redirect_uri"] == ["https://pas.example.test/auth/feishu/tenant-verification/callback"]
    assert len(params["state"][0]) >= 32

    async with factory() as session:
        states = list((await session.execute(select(FeishuTenantVerificationState))).scalars())
    assert len(states) == 1
    assert states[0].state_hash != params["state"][0]


async def test_feishu_user_login_creates_or_reuses_tenant_identity(client, setup, monkeypatch) -> None:
    http, admin_headers, _member_headers = client
    factory, _admin, _member = setup
    monkeypatch.setattr(
        "server.auth.router.get_config",
        lambda: SimpleNamespace(
            server=SimpleNamespace(public_base_url="https://pas.example.test"),
            auth=SimpleNamespace(jwt=SimpleNamespace(access_token_expire_minutes=30, refresh_token_expire_days=7)),
        ),
    )
    created = await http.post(
        "/api/identity-sources",
        json={
            "name": "Feishu login source",
            "provider": "feishu",
            "app_id": "cli_login",
            "app_secret": "secret-login",
        },
        headers=admin_headers,
    )
    source_id = created.json()["id"]
    async with factory() as session:
        source = await session.get(EnterpriseIdentitySource, source_id)
        assert source is not None
        source.tenant_id = "tenant-login"
        source.status = "active"
        await session.commit()

    started = await http.get("/auth/feishu/login")

    assert started.status_code == 303
    params = parse_qs(urlparse(started.headers["location"]).query)
    assert params["client_id"] == ["cli_login"]
    assert params["redirect_uri"] == ["https://pas.example.test/auth/feishu/login/callback"]
    state = params["state"][0]
    async with factory() as session:
        assert len(list((await session.execute(select(FeishuUserLoginState))).scalars())) == 1

    async def fake_authenticate(**_kwargs):
        return {
            "tenant_key": "tenant-login",
            "user_id": "ou_alice",
            "display_name": "Alice",
            "email": "alice@example.com",
        }

    monkeypatch.setattr("server.auth.router.authenticate_feishu_user", fake_authenticate)
    completed = await http.get(f"/auth/feishu/login/callback?code=login-code&state={state}")

    assert completed.status_code == 303
    assert completed.headers["location"] == "/dashboard"
    assert "session_token=" in completed.headers["set-cookie"]
    async with factory() as session:
        source = await session.get(EnterpriseIdentitySource, source_id)
        assert source is not None
        assert (
            await session.execute(select(EnterpriseIdentitySource).where(EnterpriseIdentitySource.id == source.id))
        ).scalar_one() is source
        assert len(list((await session.execute(select(FeishuUserLoginState))).scalars())) == 0


async def test_sharepoint_user_login_creates_or_reuses_tenant_identity(client, setup, monkeypatch) -> None:
    http, admin_headers, _member_headers = client
    factory, _admin, _member = setup
    monkeypatch.setattr(
        "server.auth.router.get_config",
        lambda: SimpleNamespace(
            server=SimpleNamespace(public_base_url="https://pas.example.test"),
            auth=SimpleNamespace(
                jwt=SimpleNamespace(
                    access_token_expire_minutes=30,
                    refresh_token_expire_days=7,
                )
            ),
        ),
    )
    created = await http.post(
        "/api/identity-sources",
        json={
            "name": "SharePoint login source",
            "provider": "sharepoint",
            "tenant_id": "tenant-login",
            "client_id": "client-login",
            "client_secret": "secret-login",
        },
        headers=admin_headers,
    )
    source_id = created.json()["id"]
    async with factory() as session:
        source = await session.get(EnterpriseIdentitySource, source_id)
        assert source is not None
        source.status = "active"
        await session.commit()

    started = await http.get("/auth/sharepoint/login")

    assert started.status_code == 303
    params = parse_qs(urlparse(started.headers["location"]).query)
    assert params["client_id"] == ["client-login"]
    assert params["redirect_uri"] == ["https://pas.example.test/auth/sharepoint/login/callback"]
    assert params["scope"] == ["openid profile email"]
    assert params["code_challenge_method"] == ["S256"]
    assert len(params["state"][0]) >= 32
    assert len(params["nonce"][0]) >= 32

    async def fake_authenticate(**_kwargs):
        return SimpleNamespace(
            subject="sharepoint-user-001",
            display_name="Alice",
            email="alice@example.com",
        )

    monkeypatch.setattr("server.auth.router.authenticate_sharepoint_user", fake_authenticate)
    completed = await http.get(f"/auth/sharepoint/login/callback?code=login-code&state={params['state'][0]}")

    assert completed.status_code == 303
    assert completed.headers["location"] == "/dashboard"
    assert completed.headers["cache-control"] == "no-store"
    assert "session_token=" in completed.headers["set-cookie"]
    async with factory() as session:
        identity = (
            await session.execute(
                select(UserExternalIdentity).where(
                    UserExternalIdentity.identity_provider == "sharepoint:tenant-login",
                    UserExternalIdentity.external_subject == "sharepoint-user-001",
                )
            )
        ).scalar_one_or_none()
        assert identity is not None


@pytest.mark.parametrize(
    ("provider", "source_payloads", "login_path", "expected_client_id"),
    [
        (
            "feishu",
            [
                {"name": "Feishu tenant A", "provider": "feishu", "app_id": "cli_a", "app_secret": "secret-a"},
                {"name": "Feishu tenant B", "provider": "feishu", "app_id": "cli_b", "app_secret": "secret-b"},
            ],
            "/auth/feishu/login",
            "cli_b",
        ),
        (
            "sharepoint",
            [
                {"name": "SharePoint tenant A", "provider": "sharepoint", "tenant_id": "tenant-a", "client_id": "client-a", "client_secret": "secret-a"},
                {"name": "SharePoint tenant B", "provider": "sharepoint", "tenant_id": "tenant-b", "client_id": "client-b", "client_secret": "secret-b"},
            ],
            "/auth/sharepoint/login",
            "client-b",
        ),
    ],
)
async def test_user_login_selects_an_active_identity_source_when_provider_has_multiple_sources(
    client,
    setup,
    monkeypatch,
    provider,
    source_payloads,
    login_path,
    expected_client_id,
) -> None:
    http, admin_headers, _member_headers = client
    factory, _admin, _member = setup
    monkeypatch.setattr(
        "server.auth.router.get_config",
        lambda: SimpleNamespace(
            server=SimpleNamespace(public_base_url="https://pas.example.test"),
            auth=SimpleNamespace(
                jwt=SimpleNamespace(
                    access_token_expire_minutes=30,
                    refresh_token_expire_days=7,
                )
            ),
        ),
    )
    source_ids = []
    for index, payload in enumerate(source_payloads):
        created = await http.post("/api/identity-sources", json=payload, headers=admin_headers)
        assert created.status_code == 201
        source_ids.append(created.json()["id"])
        async with factory() as session:
            source = await session.get(EnterpriseIdentitySource, source_ids[-1])
            assert source is not None
            source.tenant_id = f"{provider}-tenant-{index}"
            source.status = EnterpriseIdentitySourceStatus.ACTIVE
            await session.commit()

    selection = await http.get(login_path)

    assert selection.status_code == 200
    assert selection.headers["cache-control"] == "no-store"
    assert f"source_id={source_ids[0]}" in selection.text
    assert f"source_id={source_ids[1]}" in selection.text

    started = await http.get(f"{login_path}?source_id={source_ids[1]}")

    assert started.status_code == 303
    assert parse_qs(urlparse(started.headers["location"]).query)["client_id"] == [expected_client_id]


async def test_sharepoint_user_login_rejects_replayed_state(client, setup, monkeypatch) -> None:
    http, admin_headers, _member_headers = client
    factory, _admin, _member = setup
    monkeypatch.setattr(
        "server.auth.router.get_config",
        lambda: SimpleNamespace(
            server=SimpleNamespace(public_base_url="https://pas.example.test"),
            auth=SimpleNamespace(
                jwt=SimpleNamespace(
                    access_token_expire_minutes=30,
                    refresh_token_expire_days=7,
                )
            ),
        ),
    )
    created = await http.post(
        "/api/identity-sources",
        json={
            "name": "SharePoint replay source",
            "provider": "sharepoint",
            "tenant_id": "tenant-replay",
            "client_id": "client-replay",
            "client_secret": "secret-replay",
        },
        headers=admin_headers,
    )
    source_id = created.json()["id"]
    async with factory() as session:
        source = await session.get(EnterpriseIdentitySource, source_id)
        assert source is not None
        source.status = "active"
        await session.commit()

    started = await http.get("/auth/sharepoint/login")
    state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]

    async def fake_authenticate(**_kwargs):
        return SimpleNamespace(
            subject="sharepoint-user-002",
            display_name="Bob",
            email=None,
        )

    monkeypatch.setattr("server.auth.router.authenticate_sharepoint_user", fake_authenticate)
    first = await http.get(f"/auth/sharepoint/login/callback?code=login-code&state={state}")
    replay = await http.get(f"/auth/sharepoint/login/callback?code=login-code&state={state}")

    assert first.status_code == 303
    assert replay.status_code == 400
    assert replay.headers["cache-control"] == "no-store"


async def test_feishu_tenant_verification_callback_activates_source(client, setup, monkeypatch):
    http, admin_headers, _member_headers = client
    factory, _admin, _member = setup
    config = SimpleNamespace(server=SimpleNamespace(public_base_url="https://pas.example.test"))
    monkeypatch.setattr("server.api.identity_sources.get_config", lambda: config)
    monkeypatch.setattr("server.auth.router.get_config", lambda: config)
    monkeypatch.setattr(
        "server.auth.router.discover_feishu_tenant_key",
        lambda **_kwargs: _return("tenant-key-001"),
    )
    created = await http.post(
        "/api/identity-sources",
        json={
            "name": "Feishu callback verification",
            "provider": "feishu",
            "app_id": "cli_test",
            "app_secret": "secret-value",
        },
        headers=admin_headers,
    )
    started = await http.post(
        f"/api/identity-sources/{created.json()['id']}/feishu-verification",
        headers=admin_headers,
    )
    state = parse_qs(urlparse(started.json()["authorization_url"]).query)["state"][0]

    completed = await http.get(
        f"/auth/feishu/tenant-verification/callback?code=authorization-code&state={state}",
        follow_redirects=False,
    )

    assert completed.status_code == 303
    assert completed.headers["location"] == "/users?identity_source_verified=1"
    assert completed.headers["cache-control"] == "no-store"
    async with factory() as session:
        source = await session.get(EnterpriseIdentitySource, created.json()["id"])
        assert source is not None
        assert source.tenant_id == "tenant-key-001"
        assert source.status.value == "pending_binding"
        assert list((await session.execute(select(FeishuTenantVerificationState))).scalars()) == []


async def test_admin_configures_encrypted_feishu_acl_membership_snapshot(client, setup, monkeypatch):
    http, admin_headers, _member_headers = client
    factory, _admin, _member = setup
    config = SimpleNamespace(server=SimpleNamespace(public_base_url="https://pas.example.test"))
    monkeypatch.setattr("server.api.identity_sources.get_config", lambda: config)
    monkeypatch.setattr("server.auth.router.get_config", lambda: config)
    monkeypatch.setattr(
        "server.auth.router.discover_feishu_tenant_key",
        lambda **_kwargs: _return("tenant-key-001"),
    )
    created = await http.post(
        "/api/identity-sources",
        json={
            "name": "Feishu ACL snapshot",
            "provider": "feishu",
            "app_id": "cli_test",
            "app_secret": "app-secret",
        },
        headers=admin_headers,
    )
    started = await http.post(
        f"/api/identity-sources/{created.json()['id']}/feishu-verification",
        headers=admin_headers,
    )
    state = parse_qs(urlparse(started.json()["authorization_url"]).query)["state"][0]
    completed = await http.get(
        f"/auth/feishu/tenant-verification/callback?code=authorization-code&state={state}",
        follow_redirects=False,
    )
    assert completed.status_code == 303

    saved = await http.put(
        f"/api/identity-sources/{created.json()['id']}/acl-membership-snapshot",
        json={
            "host": "meta.example.test",
            "port": 3306,
            "database": "polar_rag_meta",
            "username": "readonly",
            "password": "snapshot-secret",
        },
        headers=admin_headers,
    )

    assert saved.status_code == 200
    assert saved.json()["acl_membership_snapshot_configured"] is True
    assert "snapshot-secret" not in saved.text
    async with factory() as session:
        source = await session.get(EnterpriseIdentitySource, created.json()["id"])
        assert source is not None and source.config_ciphertext is not None
        assert json.loads(decrypt(source.config_ciphertext)) == {
            "app_id": "cli_test",
            "app_secret": "app-secret",
            "acl_membership_snapshot": {
                "host": "meta.example.test",
                "port": 3306,
                "database": "polar_rag_meta",
                "username": "readonly",
                "password": "snapshot-secret",
            },
        }


async def _return(value: str) -> str:
    return value


async def test_admin_creates_encrypted_feishu_source_and_binds_enabled_space(client, setup):
    http, admin_headers, member_headers = client
    factory, admin, _member = setup

    forbidden = await http.post(
        "/api/identity-sources",
        json={
            "name": "Feishu engineering",
            "provider": "feishu",
            "app_id": "cli_test",
            "app_secret": "secret-value",
        },
        headers=member_headers,
    )
    assert forbidden.status_code == 403

    created = await http.post(
        "/api/identity-sources",
        json={
            "name": "Feishu engineering",
            "provider": "feishu",
            "app_id": "cli_test",
            "app_secret": "secret-value",
        },
        headers=admin_headers,
    )
    assert created.status_code == 201
    assert "secret-value" not in created.text
    source_id = created.json()["id"]

    async with factory() as session:
        source = await session.get(EnterpriseIdentitySource, source_id)
        assert source is not None
        assert source.config_ciphertext is not None
        assert json.loads(decrypt(source.config_ciphertext)) == {
            "app_id": "cli_test",
            "app_secret": "secret-value",
        }
        instance = PolarRAGInstance(
            name="identity-source-rag",
            scheme="https",
            host="rag.example.test",
            port=443,
            username_ciphertext="encrypted",
            password_ciphertext="encrypted",
            status=PolarRAGInstanceStatus.ACTIVE,
            created_by=admin.id,
        )
        session.add(instance)
        await session.flush()
        space = PolarRAGSpace(
            polarrag_instance_id=instance.id,
            space_id="space-a",
            name="A",
            identity_domain="domain-a",
            enabled=True,
        )
        session.add(space)
        await session.commit()
        knowledge_space_id = space.knowledge_space_id

    binding = await http.post(
        f"/api/identity-sources/{source_id}/spaces/{knowledge_space_id}",
        headers=admin_headers,
    )
    assert binding.status_code == 201
    assert binding.json()["knowledge_space_id"] == knowledge_space_id

    listed = await http.get("/api/identity-sources", headers=admin_headers)
    assert listed.status_code == 200
    assert listed.json()["items"] == [
        {
            "id": source_id,
            "name": "Feishu engineering",
            "provider": "feishu",
            "tenant_id": None,
            "status": "pending_tenant_verification",
            "last_synced_at": None,
            "last_error": None,
            "sync_supported": True,
            "acl_membership_snapshot_configured": False,
            "space_bindings": [knowledge_space_id],
        }
    ]

    unbound = await http.delete(
        f"/api/identity-sources/{source_id}/spaces/{knowledge_space_id}",
        headers=admin_headers,
    )
    assert unbound.status_code == 204

    listed_after_unbind = await http.get("/api/identity-sources", headers=admin_headers)
    assert listed_after_unbind.status_code == 200
    assert listed_after_unbind.json()["items"][0]["space_bindings"] == []


async def test_admin_can_save_sharepoint_configuration_for_sync(client, setup) -> None:
    http, admin_headers, _member_headers = client
    factory, _admin, _member = setup
    created = await http.post(
        "/api/identity-sources",
        json={
            "name": "SharePoint engineering",
            "provider": "sharepoint",
            "tenant_id": "tenant-001",
            "cloud": "china",
            "client_id": "client-id",
            "client_secret": "client-secret",
        },
        headers=admin_headers,
    )
    assert created.status_code == 201
    assert created.json()["sync_supported"] is True
    assert created.json()["cloud"] == "china"
    assert "client-secret" not in created.text

    async with factory() as session:
        source = await session.get(EnterpriseIdentitySource, created.json()["id"])
        assert source is not None
        assert json.loads(decrypt(source.config_ciphertext or "")) == {
            "cloud": "china",
            "client_id": "client-id",
            "client_secret": "client-secret",
        }


async def test_admin_can_force_sync_an_identity_source(client, setup, monkeypatch) -> None:
    http, admin_headers, _member_headers = client
    factory, _admin, _member = setup
    created = await http.post(
        "/api/identity-sources",
        json={
            "name": "Feishu directory",
            "provider": "feishu",
            "app_id": "cli_directory",
            "app_secret": "secret-directory",
        },
        headers=admin_headers,
    )
    source_id = created.json()["id"]
    async with factory() as session:
        source = await session.get(EnterpriseIdentitySource, source_id)
        assert source is not None
        source.tenant_id = "tenant-directory"
        source.status = EnterpriseIdentitySourceStatus.PENDING_BINDING
        await session.commit()

    async def fake_sync(session, source) -> None:
        assert source.id == source_id
        source.status = EnterpriseIdentitySourceStatus.ACTIVE
        source.last_error = None

    monkeypatch.setattr("server.api.identity_sources.sync_identity_source", fake_sync)

    synced = await http.post(
        f"/api/identity-sources/{source_id}/sync",
        headers=admin_headers,
    )

    assert synced.status_code == 200
    assert synced.json()["status"] == "active"


async def test_force_sync_keeps_the_sanitized_failure_state(client, setup, monkeypatch) -> None:
    http, admin_headers, _member_headers = client
    factory, _admin, _member = setup
    created = await http.post(
        "/api/identity-sources",
        json={
            "name": "Feishu directory",
            "provider": "feishu",
            "app_id": "cli_directory",
            "app_secret": "secret-directory",
        },
        headers=admin_headers,
    )
    source_id = created.json()["id"]
    async with factory() as session:
        source = await session.get(EnterpriseIdentitySource, source_id)
        assert source is not None
        source.tenant_id = "tenant-directory"
        source.status = EnterpriseIdentitySourceStatus.PENDING_BINDING
        await session.commit()

    async def fake_sync(_session, source) -> None:
        source.status = EnterpriseIdentitySourceStatus.STALE
        source.last_error = "RuntimeError"
        raise RuntimeError("provider details must not be exposed")

    monkeypatch.setattr("server.api.identity_sources.sync_identity_source", fake_sync)

    failed = await http.post(
        f"/api/identity-sources/{source_id}/sync",
        headers=admin_headers,
    )

    assert failed.status_code == 502
    assert failed.json() == {"detail": "Identity source synchronization failed"}
    async with factory() as session:
        source = await session.get(EnterpriseIdentitySource, source_id)
        assert source is not None
        assert source.status == EnterpriseIdentitySourceStatus.STALE
        assert source.last_error == "RuntimeError"
