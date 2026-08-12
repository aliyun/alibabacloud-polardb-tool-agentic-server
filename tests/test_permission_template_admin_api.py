from __future__ import annotations

pytest_plugins = ("tests._admin_api_fixtures",)


async def test_admin_creates_template_and_immutable_revision(client):
    http, admin_headers, member_headers = client
    forbidden = await http.post(
        "/api/permission-templates",
        headers=member_headers,
        json={"name": "sandbox-default", "description": "safe default"},
    )
    assert forbidden.status_code == 403

    created = await http.post(
        "/api/permission-templates",
        headers=admin_headers,
        json={"name": "sandbox-default", "description": "safe default"},
    )
    assert created.status_code == 201
    template_id = created.json()["id"]
    revision = await http.post(
        f"/api/permission-templates/{template_id}/revisions",
        headers=admin_headers,
        json={"privileges": ["SELECT", "INSERT"], "grant_option": False},
    )
    assert revision.status_code == 201
    assert revision.json()["revision"] == 1
    assert revision.json()["privileges"] == ["SELECT", "INSERT"]
    assert revision.json()["grant_option"] is False

    listed = await http.get("/api/permission-templates", headers=admin_headers)
    assert listed.status_code == 200
    assert listed.json()[0]["revisions"][0]["id"] == revision.json()["id"]
    assert "password" not in str(listed.json()).lower()


async def test_apply_sync_requires_confirmation_and_valid_target(client):
    http, admin_headers, _ = client
    template = (
        await http.post(
            "/api/permission-templates",
            headers=admin_headers,
            json={"name": "sync-api-template"},
        )
    ).json()
    revision = (
        await http.post(
            f"/api/permission-templates/{template['id']}/revisions",
            headers=admin_headers,
            json={"privileges": ["SELECT"], "grant_option": False},
        )
    ).json()
    unconfirmed = await http.post(
        f"/api/permission-template-revisions/{revision['id']}/sync",
        headers=admin_headers,
        json={
            "target_scope": "pool",
            "target_id": "missing-pool",
            "mode": "apply",
            "confirmed": False,
        },
    )
    assert unconfirmed.status_code == 409
    assert unconfirmed.json()["detail"]["code"] == "CONFIRMATION_REQUIRED"

    missing = await http.post(
        f"/api/permission-template-revisions/{revision['id']}/sync",
        headers=admin_headers,
        json={
            "target_scope": "pool",
            "target_id": "missing-pool",
            "mode": "dry_run",
            "confirmed": False,
        },
    )
    assert missing.status_code == 404
