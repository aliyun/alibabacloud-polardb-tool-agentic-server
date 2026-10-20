from __future__ import annotations

import pytest
from sqlalchemy import select

from server.models import KnowledgeResource
from server.polarrag.catalog import sync_space_catalog
from server.polarrag.contracts import (
    PolarRAGErrorCode,
    PolarRAGKnowledgeBaseRecord,
    PolarRAGUpstreamError,
)
from tests.test_polarrag_catalog import seeded as seeded, _add_active_resource


def record(kb_id):
    return PolarRAGKnowledgeBaseRecord(
        space_id="space-a",
        kb_id=kb_id,
        name=kb_id,
        kb_type="PUBLIC",
        identity_domain="tenant-a",
        owner=None,
    )


class TargetedClient:
    def __init__(self, pages, error=None):
        self.pages = pages
        self.calls = []
        self.error = error

    async def list_knowledge_bases_page(self, space_id, *, cursor, page_size, kb_ids):
        self.calls.append((space_id, cursor, page_size, kb_ids))
        if self.error:
            raise self.error
        return self.pages[cursor]


async def test_targeted_sync_pages_without_retiring_other_kbs(seeded):
    session, space, _ = seeded
    await _add_active_resource(session, space)
    client = TargetedClient({None: ([record("a")], "a"), "a": ([record("b")], None)})
    result = await sync_space_catalog(session, space, client, kb_ids=["a", "b"])
    assert result == {"active": 2, "disabled": 0, "owner_unresolved": 0, "knowledge_bases": 2}
    assert client.calls == [
        ("space-a", None, 100, ["a", "b"]),
        ("space-a", "a", 100, ["a", "b"]),
    ]
    rows = (await session.scalars(select(KnowledgeResource))).all()
    assert {row.kb_id: row.enabled for row in rows} == {"public-kb": True, "a": True, "b": True}


@pytest.mark.parametrize("failure", ["missing", "extra", "409", "503"])
async def test_targeted_failure_rolls_back_without_disabling_space_or_other_kbs(seeded, failure):
    session, space, _ = seeded
    await _add_active_resource(session, space)
    space_pk = space.knowledge_space_id
    error = (
        PolarRAGUpstreamError(PolarRAGErrorCode.UNAVAILABLE, status_code=int(failure))
        if failure in {"409", "503"}
        else None
    )
    client = TargetedClient(
        {
            None: ([record("a")], "next"),
            "next": (
                [record("other")] if failure == "extra" else [],
                None,
            ),
        },
        error,
    )
    with pytest.raises(PolarRAGUpstreamError):
        await sync_space_catalog(session, space, client, kb_ids=["a", "b"])
    rows = (await session.scalars(select(KnowledgeResource))).all()
    assert [(row.kb_id, row.enabled) for row in rows] == [("public-kb", True)]
    await session.refresh(space)
    assert space.knowledge_space_id == space_pk
    assert space.enabled is True
