from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.aliyun.polardb_client import (
    AliyunCredentialsUnavailable,
    OPENAPI_DUPLICATE_CODES,
    OpenAPIError,
    PolarDBClient,
)
from server.core.crypto import decrypt, encrypt
from server.core.dedicated_mysql import DedicatedMySQL
from server.core.dedicated_pool_repository import activate_dedicated_member
from server.core.permission_template_service import (
    PermissionScope,
    compile_permission_snapshot,
    permission_snapshot_to_json,
)
from server.core.polardb_provisioning_helpers import (
    generate_db_password,
    resolve_primary_endpoint,
)
from server.logging import safe_request_id
from server.models import (
    AllocationMode,
    DedicatedMemberStatus,
    DedicatedPoolMember,
    DedicatedPreparationStep,
    InstanceStatus,
    ReadinessStatus,
)


class DedicatedPoolProvisioningError(Exception):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _network_identifier(value: Any, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise DedicatedPoolProvisioningError(f"{field} cannot be blank")
    return normalized


def _purchase_parameters(member: DedicatedPoolMember) -> dict[str, str]:
    try:
        configured = json.loads(member.pool.purchase_config_json)
    except (TypeError, json.JSONDecodeError) as error:
        raise DedicatedPoolProvisioningError(
            "Dedicated pool purchase configuration is invalid"
        ) from error
    if not isinstance(configured, dict):
        raise DedicatedPoolProvisioningError(
            "Dedicated pool purchase configuration must be an object"
        )
    params = {str(key): str(value) for key, value in configured.items()}
    params.update(
        {
            "region_id": _network_identifier(
                member.pool.region_id, "region_id"
            ),
            "vpc_id": _network_identifier(member.pool.vpc_id, "vpc_id"),
            "vswitch_id": _network_identifier(
                member.pool.vswitch_id, "vswitch_id"
            ),
            "client_token": str(member.purchase_token),
        }
    )
    zone_id = str(member.pool.zone_id or "").strip()
    if zone_id:
        params["zone_id"] = zone_id
    if member.pool.security_ip_list:
        params["security_ip_list"] = member.pool.security_ip_list
    return params


def _is_duplicate(error: BaseException) -> bool:
    return isinstance(error, OpenAPIError) and error.code in OPENAPI_DUPLICATE_CODES


def _account_available(account: dict[str, Any] | None) -> bool:
    return bool(
        account
        and str(account.get("status") or "").strip().lower() == "available"
    )


class DedicatedPoolProvisioner:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        client: PolarDBClient | None,
        mysql: DedicatedMySQL,
    ) -> None:
        self._session_factory = session_factory
        self._client = client
        self._mysql = mysql

    def _client_or_raise(self) -> PolarDBClient:
        if self._client is None:
            raise AliyunCredentialsUnavailable(
                "Alibaba Cloud access is not configured"
            )
        return self._client

    async def advance(
        self,
        member_id: str,
        *,
        allow_data_plane: bool = True,
    ) -> DedicatedPreparationStep:
        try:
            return await self._advance_once(
                member_id,
                allow_data_plane=allow_data_plane,
            )
        except Exception as error:
            async with self._session_factory() as session:
                member = await session.get(DedicatedPoolMember, member_id)
                if member is not None:
                    member.failure_reason = _failure_record(error)
                    if isinstance(error, OpenAPIError):
                        member.cloud_request_id = safe_request_id(
                            error.request_id
                        )
                    member.retry_count += 1
                    await session.commit()
            raise

    async def delete_cluster(self, cluster_id: str) -> None:
        await self._client_or_raise().delete_cluster(cluster_id)

    async def _advance_once(
        self,
        member_id: str,
        *,
        allow_data_plane: bool,
    ) -> DedicatedPreparationStep:
        async with self._session_factory() as session:
            member = await session.get(DedicatedPoolMember, member_id)
            if member is None:
                raise DedicatedPoolProvisioningError(
                    "Dedicated pool member does not exist"
                )
            step = member.preparation_step

            if step is DedicatedPreparationStep.PENDING:
                member.purchase_token = str(uuid.uuid4())
                member.preparation_step = (
                    DedicatedPreparationStep.PURCHASE_INTENT_STORED
                )
                await session.commit()

            elif step is DedicatedPreparationStep.PURCHASE_INTENT_STORED:
                purchase_parameters = _purchase_parameters(member)
                pool_description = f"PAS Dedicated pool {member.pool.name}"
                member_description = f"PAS prewarmed member {member.id}"
                await session.commit()
                result = await self._client_or_raise().create_dedicated_cluster(
                    purchase_parameters,
                    agentic_db_type="dedicated",
                    agentic_db_cluster_id=None,
                    agentic_db_cluster_description=pool_description,
                    db_cluster_description=member_description,
                )
                cluster_id = str(result.get("cluster_id") or "")
                if not cluster_id:
                    raise DedicatedPoolProvisioningError(
                        "Dedicated cluster purchase returned no cluster ID"
                    )
                member.instance.cluster_id = cluster_id
                member.instance.region = member.pool.region_id
                member.instance.allocation_mode = AllocationMode.DEDICATED_POOL
                member.instance.status = InstanceStatus.CREATING
                member.agentic_db_cluster_id = _optional_string(
                    result.get("agentic_db_cluster_id")
                )
                member.cloud_request_id = _optional_string(
                    result.get("request_id")
                )
                member.preparation_step = (
                    DedicatedPreparationStep.PURCHASE_REQUESTED
                )
                await session.commit()

            elif step is DedicatedPreparationStep.PURCHASE_REQUESTED:
                cluster_id = member.instance.cluster_id
                await session.commit()
                result = await self._client_or_raise().describe_cluster_attribute(
                    cluster_id
                )
                if result.get("status") == "Running":
                    member.preparation_step = (
                        DedicatedPreparationStep.CLUSTER_READY
                    )
                    await session.commit()

            elif step is DedicatedPreparationStep.CLUSTER_READY:
                cluster_id = member.instance.cluster_id
                endpoint_net_type = member.pool.endpoint_net_type
                await session.commit()
                host, port = await resolve_primary_endpoint(
                    self._client_or_raise(),
                    cluster_id,
                    endpoint_net_type,
                )
                member.host = host
                member.port = port
                member.instance.host = host
                member.instance.port = port
                member.preparation_step = (
                    DedicatedPreparationStep.ENDPOINT_RESOLVED
                )
                await session.commit()

            elif step is DedicatedPreparationStep.ENDPOINT_RESOLVED:
                lifecycle_username = f"pas_lifecycle_{member.id[:8]}"
                member.lifecycle_username_ciphertext = encrypt(
                    lifecycle_username
                )
                member.lifecycle_password_ciphertext = encrypt(
                    generate_db_password()
                )
                member.preparation_step = (
                    DedicatedPreparationStep.LIFECYCLE_ACCOUNT_STORED
                )
                await session.commit()

            elif step is DedicatedPreparationStep.LIFECYCLE_ACCOUNT_STORED:
                cluster_id = member.instance.cluster_id
                lifecycle_username = decrypt(
                    str(member.lifecycle_username_ciphertext)
                )
                lifecycle_password = decrypt(
                    str(member.lifecycle_password_ciphertext)
                )
                await session.commit()
                try:
                    await self._client_or_raise().create_account(
                        cluster_id,
                        lifecycle_username,
                        lifecycle_password,
                        account_type="Super",
                    )
                except Exception as error:
                    if not _is_duplicate(error):
                        raise
                member.preparation_step = (
                    DedicatedPreparationStep.LIFECYCLE_ACCOUNT_CREATED
                )
                await session.commit()

            elif step is DedicatedPreparationStep.LIFECYCLE_ACCOUNT_CREATED:
                cluster_id = member.instance.cluster_id
                lifecycle_username = decrypt(
                    str(member.lifecycle_username_ciphertext)
                )
                await session.commit()
                account = await self._client_or_raise().describe_account(
                    cluster_id,
                    lifecycle_username,
                )
                if not _account_available(account):
                    return member.preparation_step
                member.sandbox_username_ciphertext = encrypt(
                    member.pool.account_name_template
                )
                member.sandbox_password_ciphertext = encrypt(
                    generate_db_password()
                )
                member.database_name = member.pool.database_name_template
                revision = member.pool.permission_template_revision
                snapshot = compile_permission_snapshot(
                    revision_id=revision.id,
                    template_id=revision.template_id,
                    privileges_json=revision.privileges_json,
                    grant_option=revision.grant_option,
                    scope=PermissionScope.DEDICATED,
                )
                member.permission_template_revision_id = revision.id
                member.permission_snapshot_json = permission_snapshot_to_json(
                    snapshot
                )
                member.preparation_step = (
                    DedicatedPreparationStep.SANDBOX_ACCOUNT_STORED
                )
                await session.commit()

            elif step is DedicatedPreparationStep.SANDBOX_ACCOUNT_STORED:
                cluster_id = member.instance.cluster_id
                sandbox_username = decrypt(
                    str(member.sandbox_username_ciphertext)
                )
                sandbox_password = decrypt(
                    str(member.sandbox_password_ciphertext)
                )
                await session.commit()
                try:
                    await self._client_or_raise().create_account(
                        cluster_id,
                        sandbox_username,
                        sandbox_password,
                        account_type="Normal",
                    )
                except Exception as error:
                    if not _is_duplicate(error):
                        raise
                member.preparation_step = (
                    DedicatedPreparationStep.SANDBOX_ACCOUNT_CREATED
                )
                await session.commit()

            elif step is DedicatedPreparationStep.SANDBOX_ACCOUNT_CREATED:
                cluster_id = member.instance.cluster_id
                sandbox_username = decrypt(
                    str(member.sandbox_username_ciphertext)
                )
                database_name = str(member.database_name)
                await session.commit()
                account = await self._client_or_raise().describe_account(
                    cluster_id,
                    sandbox_username,
                )
                if not _account_available(account):
                    return member.preparation_step
                try:
                    await self._client_or_raise().create_database(
                        cluster_id,
                        database_name,
                    )
                except Exception as error:
                    if not _is_duplicate(error):
                        raise
                member.preparation_step = (
                    DedicatedPreparationStep.DATABASE_CREATED
                )
                await session.commit()

            elif step is DedicatedPreparationStep.DATABASE_CREATED:
                member.preparation_step = DedicatedPreparationStep.OPENAPI_READY
                await session.commit()

            elif step is DedicatedPreparationStep.OPENAPI_READY:
                if not allow_data_plane:
                    return member.preparation_step
                await session.commit()
                await self._mysql.apply_permissions(member)
                member.preparation_step = (
                    DedicatedPreparationStep.PRIVILEGES_GRANTED
                )
                await session.commit()

            elif step is DedicatedPreparationStep.PRIVILEGES_GRANTED:
                await session.commit()
                await self._mysql.verify(member)
                now = _utcnow()
                member.last_ready_verified_at = now
                member.readiness_status = ReadinessStatus.FRESH
                member.instance.status = InstanceStatus.ACTIVE
                member.preparation_step = DedicatedPreparationStep.VERIFIED
                member.failure_reason = None
                member.retry_count = 0
                member.next_retry_at = None
                if member.allocated_resource_id is not None:
                    resource = member.allocated_resource
                    if resource is None:
                        raise DedicatedPoolProvisioningError(
                            "Allocated Dedicated resource does not exist"
                        )
                    activate_dedicated_member(member, resource)
                else:
                    member.status = DedicatedMemberStatus.AVAILABLE
                await session.commit()

            return member.preparation_step


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _failure_code(error: BaseException) -> str:
    detail = error.code if isinstance(error, OpenAPIError) else type(error).__name__
    safe = re.sub(r"[^A-Za-z0-9]+", "_", str(detail)).strip("_").upper()
    return f"DEDICATED_PREWARM_{safe or 'FAILED'}"


def _failure_record(error: BaseException) -> str:
    code = _failure_code(error)
    if not isinstance(error, OpenAPIError):
        return code
    record = {
        "code": code,
        "detail": error.message,
        "occurred_at": _utcnow().isoformat(),
    }
    if error.operation:
        record["operation"] = error.operation
    return json.dumps(record, separators=(",", ":"), sort_keys=True)
