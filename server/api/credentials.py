from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.dependencies import require_admin
from server.core import credential_service
from server.core import instance_connection
from server.core.audit_logger import log_audit
from server.core.crypto import decrypt
from server.db.engine import get_session
from server.models import (
    AuditStatus,
    CredentialCapability,
    CredentialPurpose,
    CredentialStatus,
    Instance,
    InstanceCredential,
    User,
)

router = APIRouter(prefix="/credentials", tags=["credentials"])
instance_router = APIRouter(
    prefix="/{instance_id}/credentials",
    tags=["credentials"],
)


class CreateCredentialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    purpose: Literal["provisioning_admin", "direct_access"]
    capability: Literal["readonly", "readwrite", "admin"]
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=1024)
    database_name: str | None = Field(default=None, max_length=255)


class TestCredentialConnectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose: Literal["provisioning_admin", "direct_access"]
    capability: Literal["readonly", "readwrite", "admin"]
    credential_id: str | None = Field(default=None, min_length=1, max_length=36)
    expected_version: int | None = Field(default=None, ge=1)
    username: str | None = Field(default=None, min_length=1, max_length=255)
    password: str | None = Field(default=None, min_length=1, max_length=1024)
    database_name: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def require_new_credential_secrets(self):
        if self.credential_id is None and (
            self.username is None or self.password is None
        ):
            raise ValueError(
                "username and password are required for a new credential"
            )
        if self.credential_id is not None and self.expected_version is None:
            raise ValueError(
                "expected_version is required for an existing credential"
            )
        return self


class CredentialResponse(BaseModel):
    id: str
    instance_id: str | None
    resource_id: str | None
    name: str
    purpose: CredentialPurpose
    capability: CredentialCapability
    database_name: str | None
    status: CredentialStatus
    version: int
    created_by_user_id: str | None
    created_at: datetime
    updated_at: datetime | None

    @classmethod
    def from_model(
        cls, credential: InstanceCredential
    ) -> "CredentialResponse":
        return cls.model_validate(credential, from_attributes=True)


class CredentialRevealResponse(BaseModel):
    username: str
    password: str
    database_name: str | None


class CredentialRevealRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmed: Literal[True]


class UpdateCredentialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=255)
    capability: Literal["readonly", "readwrite", "admin"]
    username: str | None = Field(default=None, min_length=1, max_length=255)
    password: str | None = Field(default=None, min_length=1, max_length=1024)
    database_name: str | None = Field(default=None, max_length=255)

    @field_validator("name")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Value cannot be blank")
        return stripped

    @field_validator("database_name")
    @classmethod
    def strip_optional_database(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


async def _required_audit(
    session: AsyncSession,
    *,
    admin: User,
    action: str,
    credential: InstanceCredential,
) -> None:
    await log_audit(
        session,
        user_id=admin.id,
        instance_id=credential.instance_id,
        action=action,
        status=AuditStatus.SUCCESS,
        user_name=admin.display_name,
        target_type="instance_credential",
        target_id=credential.id,
        required=True,
        commit=False,
    )


@instance_router.get("", response_model=list[CredentialResponse])
async def list_instance_credentials(
    instance_id: str,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        rows = await credential_service.list_instance_credentials(
            session, instance_id
        )
    except credential_service.CredentialNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return [CredentialResponse.from_model(row) for row in rows]


@instance_router.post(
    "",
    response_model=CredentialResponse,
    status_code=201,
)
async def create_instance_credential(
    instance_id: str,
    body: CreateCredentialRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        credential = await credential_service.create_instance_credential(
            session,
            instance_id=instance_id,
            name=body.name,
            purpose=CredentialPurpose(body.purpose),
            capability=CredentialCapability(body.capability),
            username=body.username,
            password=body.password,
            database_name=body.database_name,
            created_by_user_id=admin.id,
        )
        await _required_audit(
            session,
            admin=admin,
            action="credential.create",
            credential=credential,
        )
        await session.commit()
        await session.refresh(credential)
        return CredentialResponse.from_model(credential)
    except credential_service.CredentialNotFound as exc:
        await session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except credential_service.CredentialValidationError as exc:
        await session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except instance_connection.ConnectionTestError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="Credential name already exists for this instance",
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        await session.rollback()
        raise HTTPException(
            status_code=503,
            detail="Credential administration unavailable",
        ) from exc


@instance_router.post("/test-connection")
async def test_instance_credential_connection(
    instance_id: str,
    body: TestCredentialConnectionRequest,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    instance = await session.get(Instance, instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail="Instance not found")
    try:
        username = body.username
        password = body.password
        if body.credential_id is not None:
            credential = await session.get(
                InstanceCredential,
                body.credential_id,
            )
            if (
                credential is None
                or credential.instance_id != instance_id
                or credential.status != CredentialStatus.ACTIVE
                or credential.username_ciphertext is None
                or credential.password_ciphertext is None
                or credential.purpose != CredentialPurpose(body.purpose)
            ):
                raise credential_service.CredentialValidationError(
                    "The selected credential is not valid for this instance"
                )
            if credential.version != body.expected_version:
                raise credential_service.CredentialVersionConflict(
                    "Credential was changed by another administrator"
                )
            username = username or decrypt(credential.username_ciphertext)
            password = password or decrypt(credential.password_ciphertext)
        assert username is not None
        assert password is not None
        await credential_service.test_credential_connection(
            instance,
            purpose=CredentialPurpose(body.purpose),
            capability=CredentialCapability(body.capability),
            username=username,
            password=password,
            database_name=body.database_name,
        )
    except credential_service.CredentialVersionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "CREDENTIAL_VERSION_CONFLICT",
                "message": str(exc),
            },
        ) from exc
    except credential_service.CredentialValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except instance_connection.ConnectionTestError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    return {"ok": True}


@router.post("/{credential_id}/reveal")
async def reveal_credential(
    credential_id: str,
    _body: CredentialRevealRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        username, password, database_name = (
            await credential_service.reveal_credential(
                session,
                credential_id,
                admin_id=admin.id,
            )
        )
        credential = await session.get(InstanceCredential, credential_id)
        if credential is None:
            raise credential_service.CredentialNotFound(
                "Credential not found"
            )
        await _required_audit(
            session,
            admin=admin,
            action="credential.reveal",
            credential=credential,
        )
        await session.commit()
        payload = CredentialRevealResponse(
            username=username,
            password=password,
            database_name=database_name,
        )
        return JSONResponse(
            content=payload.model_dump(mode="json"),
            headers={"Cache-Control": "no-store"},
        )
    except credential_service.RevealRateLimitExceeded as exc:
        await session.rollback()
        raise HTTPException(
            status_code=429,
            detail={
                "code": "RATE_LIMITED",
                "message": "Credential reveal rate limit exceeded",
            },
        ) from exc
    except credential_service.CredentialNotFound as exc:
        await session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except credential_service.CredentialUnavailable as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        await session.rollback()
        raise HTTPException(
            status_code=503,
            detail="Credential reveal unavailable",
        ) from exc


@router.put(
    "/{credential_id}",
    response_model=CredentialResponse,
)
async def update_credential(
    credential_id: str,
    body: UpdateCredentialRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        credential = await credential_service.update_instance_credential(
            session,
            credential_id=credential_id,
            expected_version=body.expected_version,
            name=body.name,
            capability=CredentialCapability(body.capability),
            username=body.username,
            password=body.password,
            database_name=body.database_name,
        )
        await _required_audit(
            session,
            admin=admin,
            action="credential.update",
            credential=credential,
        )
        await session.commit()
        await session.refresh(credential)
        return CredentialResponse.from_model(credential)
    except credential_service.CredentialVersionConflict as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "CREDENTIAL_VERSION_CONFLICT",
                "message": str(exc),
            },
        ) from exc
    except credential_service.CredentialNotFound as exc:
        await session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except credential_service.CredentialUnavailable as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except credential_service.CredentialValidationError as exc:
        await session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except instance_connection.ConnectionTestError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="Credential name already exists for this instance",
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        await session.rollback()
        raise HTTPException(
            status_code=503,
            detail="Credential administration unavailable",
        ) from exc


@router.post(
    "/{credential_id}/revoke",
    response_model=CredentialResponse,
)
async def revoke_credential(
    credential_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        credential = await credential_service.revoke_credential(
            session, credential_id
        )
        await _required_audit(
            session,
            admin=admin,
            action="credential.revoke",
            credential=credential,
        )
        await session.commit()
        await session.refresh(credential)
        return CredentialResponse.from_model(credential)
    except credential_service.CredentialNotFound as exc:
        await session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        await session.rollback()
        raise HTTPException(
            status_code=503,
            detail="Credential administration unavailable",
        ) from exc
