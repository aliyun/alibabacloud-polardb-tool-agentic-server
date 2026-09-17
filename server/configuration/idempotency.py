from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from server.configuration.repository import (
    ConfigRepository,
    ManagedMutation,
    ReceiptConflict,
    ReceiptInProgress,
    ReceiptLeaseLost,
)
from server.core.config_crypto import ConfigCrypto
from server.models import ConfigReceiptStatus


DEFAULT_LEASE_DURATION = timedelta(minutes=2)
DEFAULT_RECEIPT_LIFETIME = timedelta(hours=24)


class ManagedIdempotencyError(ValueError):
    def __init__(
        self,
        code: str,
        *,
        retry_after_seconds: int = 0,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True, slots=True)
class ManagedReceiptClaim:
    status: str
    receipt_id: str
    idempotency_key_hash: str
    request_digest: str
    lease_owner: str | None
    lease_token: str | None
    response: dict[str, Any] | None = None


class ManagedCommandIdempotency:
    def __init__(
        self,
        repository: ConfigRepository,
        crypto: ConfigCrypto,
    ) -> None:
        self.repository = repository
        self.crypto = crypto

    async def claim(
        self,
        *,
        actor_scope: str,
        idempotency_key: str,
        request: Mapping[str, Any],
        action: str,
        module: str | None,
        instance_id: str,
        instance_generation: int,
        lease_owner: str,
        now: datetime | None = None,
        lease_duration: timedelta = DEFAULT_LEASE_DURATION,
    ) -> ManagedReceiptClaim:
        if not idempotency_key:
            raise ManagedIdempotencyError("IDEMPOTENCY_KEY_REQUIRED")
        current_time = now or datetime.now(timezone.utc)
        key_hash = self.crypto.managed_digest(
            "idempotency-key-v1", idempotency_key
        )
        request_digest = self.crypto.managed_digest(
            "request-v1", request
        )
        lease_token = f"{lease_owner[:222]}:{uuid4().hex}"
        try:
            receipt = await self.repository.claim_managed_receipt(
                actor_scope=actor_scope,
                idempotency_key_hash=key_hash,
                action=action,
                module=module,
                request_digest=request_digest,
                expires_at=current_time + DEFAULT_RECEIPT_LIFETIME,
                lease_owner=lease_token,
                lease_expires_at=current_time + lease_duration,
                instance_id=instance_id,
                instance_generation=instance_generation,
                now=current_time,
            )
        except ReceiptConflict as error:
            raise ManagedIdempotencyError(
                "IDEMPOTENCY_CONFLICT"
            ) from error
        except ReceiptInProgress as error:
            raise ManagedIdempotencyError(
                "OPERATION_IN_PROGRESS",
                retry_after_seconds=error.retry_after_seconds,
            ) from error
        if receipt.status == ConfigReceiptStatus.SUCCEEDED:
            try:
                response = json.loads(receipt.response_json)
            except (TypeError, ValueError) as error:
                raise ManagedIdempotencyError(
                    "IDEMPOTENCY_RECEIPT_INVALID"
                ) from error
            if not isinstance(response, dict):
                raise ManagedIdempotencyError(
                    "IDEMPOTENCY_RECEIPT_INVALID"
                )
            return ManagedReceiptClaim(
                status="REPLAY",
                receipt_id=receipt.id,
                idempotency_key_hash=key_hash,
                request_digest=request_digest,
                lease_owner=None,
                lease_token=None,
                response=response,
            )
        return ManagedReceiptClaim(
            status="CLAIMED",
            receipt_id=receipt.id,
            idempotency_key_hash=key_hash,
            request_digest=request_digest,
            lease_owner=lease_owner,
            lease_token=lease_token,
        )

    async def complete(
        self,
        claim: ManagedReceiptClaim,
        *,
        lease_owner: str,
        mutation: ManagedMutation,
    ) -> dict[str, Any]:
        if claim.status != "CLAIMED":
            if claim.response is None:
                raise ManagedIdempotencyError(
                    "IDEMPOTENCY_RECEIPT_INVALID"
                )
            return claim.response
        if claim.lease_owner != lease_owner or claim.lease_token is None:
            raise ManagedIdempotencyError("OPERATION_IN_PROGRESS")
        try:
            return await self.repository.complete_managed_receipt(
                receipt_id=claim.receipt_id,
                request_digest=claim.request_digest,
                lease_owner=claim.lease_token,
                mutation=mutation,
            )
        except ReceiptConflict as error:
            raise ManagedIdempotencyError(
                "IDEMPOTENCY_CONFLICT"
            ) from error
        except ReceiptLeaseLost as error:
            raise ManagedIdempotencyError(
                "OPERATION_IN_PROGRESS"
            ) from error

    async def abandon(
        self,
        claim: ManagedReceiptClaim,
        *,
        lease_owner: str,
    ) -> None:
        if claim.status != "CLAIMED":
            return
        if claim.lease_owner != lease_owner or claim.lease_token is None:
            raise ManagedIdempotencyError("OPERATION_IN_PROGRESS")
        try:
            await self.repository.abandon_managed_receipt(
                receipt_id=claim.receipt_id,
                request_digest=claim.request_digest,
                lease_owner=claim.lease_token,
            )
        except ReceiptLeaseLost as error:
            raise ManagedIdempotencyError(
                "OPERATION_IN_PROGRESS"
            ) from error


def sanitized_result(value: BaseModel) -> dict[str, Any]:
    return value.model_dump(mode="json", exclude_none=True)
