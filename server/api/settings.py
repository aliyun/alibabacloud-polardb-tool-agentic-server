from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.dependencies import require_admin
from server.core import settings_manager
from server.db.engine import get_session
from server.models import User

router = APIRouter(prefix="/settings", tags=["settings"])


class UpdateSettingRequest(BaseModel):
    value: str


class BatchUpdateRequest(BaseModel):
    settings: dict[str, str]


@router.get("")
async def list_settings(
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    return await settings_manager.get_all_settings(session)


@router.put("/{key}")
async def update_setting(
    key: str,
    body: UpdateSettingRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        await settings_manager.set_setting(session, key, body.value)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"key": key, "value": body.value}


@router.post("/batch")
async def batch_update(
    body: BatchUpdateRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        await settings_manager.batch_update_settings(session, body.settings)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"updated": len(body.settings)}


@router.post("/test-credentials")
async def test_credentials(
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    from server.aliyun.polardb_client import get_polardb_client_async, MockPolarDBClient
    client = await get_polardb_client_async(session)
    if isinstance(client, MockPolarDBClient):
        raise HTTPException(status_code=400, detail="No cloud credentials configured")
    try:
        region = await settings_manager.get_setting(session, "pool_region_id") or "cn-hangzhou"
        await client.discover_clusters(region)
        return {"status": "connected", "message": f"Credentials verified successfully (region: {region})"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Connection failed: {e}")
