from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.dependencies import require_admin
from server.db.engine import get_session
from server.models import User
from server.models.quota_counter import QuotaCounter

router = APIRouter(prefix="/quota", tags=["quota"])


def _validate_quota_limit(new_limit: int, current_count: int) -> None:
    if new_limit < current_count:
        raise ValueError(
            f"Quota limit cannot be less than current usage (current: {current_count})"
        )


class UpdateGlobalQuotaRequest(BaseModel):
    max_limit: int


@router.get("/status")
async def get_quota_status(
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    rows = (await session.execute(select(QuotaCounter))).scalars().all()
    global_data = {"limit": None, "current": 0}
    departments: list[dict] = []
    for row in rows:
        if row.scope == "global":
            global_data = {"limit": row.max_limit, "current": row.current_count}
        elif row.scope.startswith("dept:"):
            dept_id = row.scope.removeprefix("dept:")
            departments.append({
                "id": dept_id, "limit": row.max_limit, "current": row.current_count,
            })
    return {"global": global_data, "departments": departments}


@router.put("/global")
async def update_global_quota(
    body: UpdateGlobalQuotaRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    row = await session.execute(
        select(QuotaCounter).where(QuotaCounter.scope == "global")
    )
    counter = row.scalar_one_or_none()
    if counter is None:
        raise HTTPException(status_code=500, detail="Global quota counter not found")
    try:
        _validate_quota_limit(body.max_limit, counter.current_count)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    counter.max_limit = body.max_limit
    await session.commit()
    return {"limit": counter.max_limit, "current": counter.current_count}
