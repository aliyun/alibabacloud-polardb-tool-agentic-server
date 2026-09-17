from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.dependencies import get_current_user
from server.auth.personal_access import PERSONAL_MCP_PATH
from server.config import get_config
from server.core import personal_token_service as tokens
from server.core.audit_logger import log_audit
from server.db.engine import get_session
from server.models import AuditStatus, User

router = APIRouter(prefix="/me/personal-tokens", tags=["personal-access"])


class IssueTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expires_in_days: int = Field(default=90, ge=1, le=365)


def summary(row, user):
    return {
        "id": row.token_family,
        "status": tokens.status(row, user),
        "created_at": row.created_at,
        "expires_at": row.expires_at,
        "revoked_at": row.revoked_at,
    }


@router.get("")
async def list_personal_tokens(
    response: Response, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)
):
    response.headers["Cache-Control"] = "no-store"
    return {
        "items": [summary(row, user) for row in await tokens.list_tokens(session, user.id)],
        "mcp_url": get_config().server.public_base_url.rstrip("/") + PERSONAL_MCP_PATH,
        "oauth_ready": bool(get_config().server.public_base_url),
    }


@router.post("", status_code=201)
async def issue_personal_token(
    body: IssueTokenRequest,
    response: Response,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    try:
        row, plaintext = await tokens.issue(
            session,
            user.id,
            resource=get_config().server.public_base_url.rstrip("/") + PERSONAL_MCP_PATH,
            expires_in_days=body.expires_in_days,
        )
        await log_audit(
            session,
            user_id=user.id,
            user_name=user.display_name,
            action="personal_token.issue",
            status=AuditStatus.SUCCESS,
            target_type="personal_token",
            target_id=row.token_family,
            required=True,
            commit=False,
        )
        await session.commit()
    except tokens.ActiveTokenExists as exc:
        await session.rollback()
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        await session.rollback()
        raise HTTPException(503, "Personal credential administration unavailable") from exc
    response.headers["Cache-Control"] = "no-store"
    return {**summary(row, user), "token": plaintext}


@router.delete("/{token_id}", status_code=204)
async def revoke_personal_token(
    token_id: str, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)
):
    try:
        row = await tokens.revoke(session, user.id, token_id)
        await log_audit(
            session,
            user_id=user.id,
            user_name=user.display_name,
            action="personal_token.revoke",
            status=AuditStatus.SUCCESS,
            target_type="personal_token",
            target_id=row.token_family,
            required=True,
            commit=False,
        )
        await session.commit()
    except LookupError as exc:
        await session.rollback()
        raise HTTPException(404, "Personal token not found") from exc
    except Exception as exc:
        await session.rollback()
        raise HTTPException(503, "Personal credential administration unavailable") from exc
    return Response(status_code=204, headers={"Cache-Control": "no-store"})
