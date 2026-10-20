from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.api.polarrag import SpaceSyncStatus, _schedule_space_sync, _space_sync_status
from server.auth.dependencies import require_admin
from server.db.engine import get_session
from server.models import PolarRAGSpace, User

router = APIRouter(prefix="/v1/admin/spaces", tags=["admin-space-sync"])


class SpaceSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kb_ids: list[str] = Field(default_factory=list, min_length=1, max_length=100)

    @field_validator("kb_ids")
    @classmethod
    def normalize_kb_ids(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 256 for value in values):
            raise ValueError("KB IDs must contain 1 to 256 non-blank characters")
        return list(dict.fromkeys(value.strip() for value in values))


async def _registered_space(session: AsyncSession, space_id: str) -> PolarRAGSpace:
    spaces = (await session.scalars(select(PolarRAGSpace).where(PolarRAGSpace.space_id == space_id).limit(2))).all()
    if not spaces:
        raise HTTPException(404, "Registered Space not found")
    if len(spaces) > 1:
        raise HTTPException(
            409,
            {
                "code": "AMBIGUOUS_SPACE",
                "message": "Space is registered under multiple instances; use the instance-qualified sync endpoint.",
            },
        )
    return spaces[0]


@router.post("/{space_id}/sync", response_model=SpaceSyncStatus, status_code=202)
async def sync_registered_space(
    space_id: str,
    request: Request,
    body: SpaceSyncRequest | None = None,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    space = await _registered_space(session, space_id)
    if not space.enabled:
        raise HTTPException(409, "Space is disabled")
    status, task = await _schedule_space_sync(
        async_sessionmaker(session.bind, expire_on_commit=False),
        session,
        space,
        admin.id,
        body.kb_ids if body and body.kb_ids else None,
    )
    if task is None:
        raise HTTPException(409, "Space synchronization is already running or Space is disabled")
    tasks = getattr(request.app.state, "background_tasks", None)
    if tasks is not None:
        tasks.add(task)
        task.add_done_callback(tasks.discard)
    return status


@router.get("/{space_id}/sync", response_model=SpaceSyncStatus)
async def get_registered_space_sync(
    space_id: str,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    return _space_sync_status(await _registered_space(session, space_id))
