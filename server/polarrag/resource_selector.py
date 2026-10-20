"""Resolve external KB coordinates before applying the caller's existing access policy."""
from __future__ import annotations

from collections.abc import Iterable

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.models import KnowledgeResource


class KnowledgePair(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    space_id: str = Field(min_length=1, max_length=255)
    kb_id: str = Field(min_length=1, max_length=255)


async def knowledge_resource_pairs(
    session: AsyncSession,
    resource_ids: Iterable[str],
) -> dict[str, dict[str, str]]:
    ids = sorted(set(resource_ids))
    pairs: dict[str, dict[str, str]] = {}
    for offset in range(0, len(ids), 500):
        rows = await session.execute(
            select(KnowledgeResource.id, KnowledgeResource.space_id, KnowledgeResource.kb_id)
            .where(KnowledgeResource.id.in_(ids[offset:offset + 500]))
        )
        pairs.update((resource_id, {"space_id": space_id, "kb_id": kb_id}) for resource_id, space_id, kb_id in rows)
    return pairs


async def resolve_knowledge_pair(
    session: AsyncSession,
    space_id: str,
    kb_id: str,
    *,
    instance_ids: list[str] | None = None,
) -> str:
    query = select(KnowledgeResource.id).where(
        KnowledgeResource.space_id == space_id, KnowledgeResource.kb_id == kb_id,
    )
    if instance_ids is not None:
        query = query.where(KnowledgeResource.polarrag_instance_id.in_(instance_ids))
    ids = list(await session.scalars(query.limit(2)))
    if len(ids) > 1:
        raise HTTPException(409, "Ambiguous Space/KB target; use knowledge_resource_id")
    if not ids:
        raise HTTPException(404, "Knowledge resource not found")
    return ids[0]
