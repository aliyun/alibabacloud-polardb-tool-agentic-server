from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from server.models import (
    AuditLog,
    AuditStatus,
    KnowledgeBindingMode,
    KnowledgeResource,
    KnowledgeResourceSyncStatus,
    PolarRAGInstance,
    PolarRAGInstanceStatus,
    PolarRAGSpace,
)

pytest_plugins = ("tests._admin_api_fixtures",)


async def _seed_audit_logs(setup) -> None:
    factory, admin, _ = setup
    async with factory() as session:
        instance = PolarRAGInstance(
            id="10000000-0000-0000-0000-000000000001",
            name="Primary RAG",
            scheme="https",
            host="rag.example.test",
            port=9200,
            username_ciphertext="encrypted-user",
            password_ciphertext="encrypted-password",
            status=PolarRAGInstanceStatus.ACTIVE,
            created_by=admin.id,
        )
        session.add(instance)
        await session.flush()
        space = PolarRAGSpace(
            knowledge_space_id="20000000-0000-0000-0000-000000000002",
            polarrag_instance_id=instance.id,
            space_id="space-a",
            name="Finance Space",
            identity_domain="tenant-a",
            enabled=True,
        )
        session.add(space)
        await session.flush()
        resource = KnowledgeResource(
            id="30000000-0000-0000-0000-000000000003",
            knowledge_space_id=space.knowledge_space_id,
            polarrag_instance_id=instance.id,
            space_id=space.space_id,
            kb_id="kb-a",
            name="Finance KB",
            kb_type="PUBLIC",
            identity_domain="tenant-a",
            binding_mode=KnowledgeBindingMode.DOMAIN,
            sync_status=KnowledgeResourceSyncStatus.ACTIVE,
            enabled=True,
        )
        session.add(resource)
        await session.flush()
        session.add_all(
            [
                AuditLog(
                    actor_user_id=admin.id,
                    action="polarrag.kb_search",
                    target_type="knowledge_resource",
                    target_id=resource.id,
                    status=AuditStatus.SUCCESS,
                    duration_ms=123,
                    metadata_json=json.dumps(
                        {
                            "client_info": json.dumps(
                                {
                                    "knowledge_resource_ids": [resource.id],
                                    "polarrag_instance_ids": [instance.id],
                                    "space_ids": [space.space_id],
                                    "kb_ids": [resource.kb_id],
                                    "hit_count": 7,
                                    "partial_failure_count": 1,
                                    "polarrag_status": "success",
                                }
                            )
                        }
                    ),
                ),
                AuditLog(
                    actor_user_id=admin.id,
                    action="run_sql",
                    status=AuditStatus.SUCCESS,
                    duration_ms=9,
                    metadata_json=json.dumps(
                        {"sql_text": "SELECT 1", "row_count": 1}
                    ),
                ),
            ]
        )
        await session.commit()


async def test_polarrag_audit_category_returns_resolved_catalog_context(
    client,
    setup,
) -> None:
    http, admin_headers, _ = client
    await _seed_audit_logs(setup)

    response = await http.get(
        "/api/audit-logs",
        params={"category": "polarrag"},
        headers=admin_headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    item = payload["items"][0]
    assert item["category"] == "polarrag"
    assert item["user_name"] == "Admin"
    assert item["action"] == "polarrag.kb_search"
    assert item["polarrag"] == {
        "instance_ids": ["10000000-0000-0000-0000-000000000001"],
        "instance_names": ["Primary RAG"],
        "space_ids": ["space-a"],
        "space_names": ["Finance Space"],
        "kb_ids": ["kb-a"],
        "kb_names": ["Finance KB"],
        "knowledge_resource_ids": [
            "30000000-0000-0000-0000-000000000003"
        ],
        "knowledge_resource_names": ["Finance KB"],
        "hit_count": 7,
        "successful_searches": None,
        "failed_searches": None,
        "partial_failure_count": 1,
        "polarrag_status": "success",
    }


async def test_sql_audit_category_filters_before_pagination(
    client,
    setup,
) -> None:
    http, admin_headers, _ = client
    await _seed_audit_logs(setup)

    response = await http.get(
        "/api/audit-logs",
        params={"category": "sql", "limit": 1},
        headers=admin_headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["category"] == "sql"
    assert payload["items"][0]["action"] == "run_sql"


async def test_audit_category_count_honors_created_from(
    client,
    setup,
) -> None:
    http, admin_headers, _ = client
    await _seed_audit_logs(setup)

    response = await http.get(
        "/api/audit-logs",
        params={
            "category": "polarrag",
            "created_from": (
                datetime.now(UTC) + timedelta(minutes=1)
            ).isoformat(),
            "limit": 1,
        },
        headers=admin_headers,
    )

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0}


async def test_audit_category_accepts_form_decoded_positive_offset(
    client,
    setup,
) -> None:
    http, admin_headers, _ = client
    await _seed_audit_logs(setup)

    response = await http.get(
        "/api/audit-logs?category=polarrag&created_from=2026-08-12T00:00:00+08:00&limit=1",
        headers=admin_headers,
    )

    assert response.status_code == 200


async def test_audit_category_preserves_local_datetime_without_offset(
    client,
    setup,
) -> None:
    http, admin_headers, _ = client
    await _seed_audit_logs(setup)

    response = await http.get(
        "/api/audit-logs",
        params={
            "category": "polarrag",
            "created_from": "2026-08-12 00:00",
            "limit": 1,
        },
        headers=admin_headers,
    )

    assert response.status_code == 200
