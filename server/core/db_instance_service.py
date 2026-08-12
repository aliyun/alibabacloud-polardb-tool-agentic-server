from __future__ import annotations

import hashlib
import json
import re
import secrets
import string
import unicodedata
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.principal import Principal, PrincipalKind
from server.core.crypto import encrypt
from server.config import get_config
from server.core.dedicated_pool_repository import (
    PoolCapacityLimitReached,
    PurchaseProfileUpgradeRequired,
    activate_dedicated_member,
    claim_fresh_member,
    consume_agent_operation_budget,
    reserve_member_purchase,
)
from server.core.db_instance_contract import (
    resource_connection_details,
    resource_capabilities,
)
from server.core.polardb_provisioning_helpers import generate_db_password
from server.core.provisioning_backend_repository import list_candidates
from server.core.provisioning_backend_repository import (
    backend_is_fresh_and_healthy,
)
from server.core.permission_template_service import (
    PermissionScope,
    default_permission_snapshot,
    effective_delete_cooldown_hours,
    permission_snapshot_to_json,
)
from server.core.provisioning_operation_budget import RateLimited
from server.core.provisioning_capacity import (
    CapacityUnavailable,
    reserve_active_capacity_counters,
    reserve_capacity_and_insert,
)
from server.core.resource_write_guard import (
    require_idle_resource_session,
    serialized_resource_write,
)
from server.models import (
    Agent,
    AgentProvisioningBinding,
    AgentStatus,
    CredentialCapability,
    CredentialPurpose,
    CredentialStatus,
    DBInstanceResource,
    DBInstanceStatus,
    DedicatedMemberStatus,
    DedicatedPoolMember,
    DedicatedPoolStatus,
    DedicatedPreparationStep,
    DeleteLifecycleStep,
    Instance,
    InstanceCredential,
    InstanceEngine,
    InstanceStatus,
    InstanceTopology,
    ProvisioningMode,
    ProvisioningBackend,
    ProvisioningBackendStatus,
    ProvisioningBackendType,
    ReadinessStatus,
    AllocationMode,
    ReclaimPolicy,
)
from server.models.base import utc_now

CLIENT_TOKEN_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_RESOURCE_ALPHABET = string.ascii_lowercase + string.digits
_FINGERPRINT_VERSION = 2


class DBInstanceServiceError(Exception):
    code = "DB_INSTANCE_ERROR"


class InvalidClientToken(DBInstanceServiceError):
    code = "INVALID_CLIENT_TOKEN"


class UnsupportedDBType(DBInstanceServiceError):
    code = "UNSUPPORTED_DB_TYPE"


class IdempotencyConflict(DBInstanceServiceError):
    code = "IDEMPOTENCY_CONFLICT"


class NoProvisioningBackend(DBInstanceServiceError):
    code = "NO_PROVISIONING_BACKEND"


class CapacityExhausted(DBInstanceServiceError):
    code = "CAPACITY_EXHAUSTED"


class DBInstanceNotFound(DBInstanceServiceError):
    code = "DB_INSTANCE_NOT_FOUND"


def validate_client_token(client_token: str) -> None:
    if (
        not isinstance(client_token, str)
        or CLIENT_TOKEN_RE.fullmatch(client_token) is None
    ):
        raise InvalidClientToken(
            "client_token must be 1-128 characters using letters, numbers, "
            "'.', '_', ':', or '-'"
        )


def normalize_resource_name(name: str | None) -> str | None:
    if name is None:
        return None
    if not isinstance(name, str):
        raise ValueError("name must be a string or null")
    normalized = unicodedata.normalize("NFC", name).strip()
    if not 1 <= len(normalized) <= 128:
        raise ValueError(
            "name must be 1-128 Unicode code points after normalization"
        )
    if any(unicodedata.category(character) == "Cc" for character in normalized):
        raise ValueError("name must not contain control characters")
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("name must be valid Unicode") from error
    return normalized


def request_fingerprint(
    db_type: str,
    name: str | None,
    provisioning_mode: ProvisioningMode = ProvisioningMode.MULTITENANT,
    version: int = _FINGERPRINT_VERSION,
) -> str:
    if version not in {1, _FINGERPRINT_VERSION}:
        raise ValueError(f"Unsupported request fingerprint version: {version}")
    normalized_name = normalize_resource_name(name)
    if version == 1:
        if provisioning_mode != ProvisioningMode.MULTITENANT:
            raise IdempotencyConflict(
                "Legacy idempotency keys support multitenant mode only"
            )
        fingerprint_payload = {
            "db_type": db_type,
            "name": normalized_name,
        }
    else:
        fingerprint_payload = {
            "db_type": db_type,
            "name": normalized_name,
            "provisioning_mode": provisioning_mode.value,
        }
    payload = json.dumps(
        fingerprint_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _parse_engine(db_type: str) -> InstanceEngine:
    if db_type != InstanceEngine.POLARDB_MYSQL.value:
        raise UnsupportedDBType(
            f"Unsupported db_type: {db_type!r}; expected 'polardb_mysql'"
        )
    return InstanceEngine.POLARDB_MYSQL


async def _find_by_client_token(
    session: AsyncSession,
    agent_id: str,
    client_token: str,
) -> DBInstanceResource | None:
    return (
        await session.execute(
            select(DBInstanceResource).where(
                DBInstanceResource.owner_agent_id == agent_id,
                DBInstanceResource.client_token == client_token,
            )
        )
    ).scalar_one_or_none()


def _return_if_same_request(
    resource: DBInstanceResource,
    *,
    fingerprint: str,
    fingerprint_version: int,
) -> DBInstanceResource:
    if (
        resource.fingerprint_version != fingerprint_version
        or resource.request_fingerprint != fingerprint
    ):
        raise IdempotencyConflict(
            "client_token was already used for a different request"
        )
    return resource


async def _commit_idempotent_replay(
    session: AsyncSession,
    *,
    agent_id: str,
    client_token: str,
    fingerprint: str,
    fingerprint_version: int,
    before_commit: (
        Callable[[AsyncSession, DBInstanceResource], Awaitable[None]] | None
    ),
) -> DBInstanceResource:
    """Re-read and audit an idempotent response under the mutation guard."""
    if session.in_transaction():
        await session.rollback()
    async with serialized_resource_write(session):
        resource = await _find_by_client_token(
            session, agent_id, client_token
        )
        if resource is None:
            raise DBInstanceNotFound("Database instance not found")
        resource = _return_if_same_request(
            resource,
            fingerprint=fingerprint,
            fingerprint_version=fingerprint_version,
        )
        if before_commit is not None:
            await before_commit(session, resource)
        await session.commit()
        return resource


def _new_database_identity() -> tuple[str, str, str, str]:
    tenant_name = "t" + "".join(
        secrets.choice(_RESOURCE_ALPHABET) for _ in range(9)
    )
    resource_config_name = f"rc_{tenant_name}"
    account_name = f"agentic@{tenant_name}"
    return tenant_name, resource_config_name, account_name, account_name


def _build_resource(
    *,
    agent_id: str,
    backend_id: str,
    client_token: str,
    fingerprint: str,
    normalized_name: str | None,
    engine: InstanceEngine,
    provisioning_mode: ProvisioningMode = ProvisioningMode.MULTITENANT,
) -> DBInstanceResource:
    tenant_name, resource_config_name, account_name, database_name = (
        _new_database_identity()
    )
    password = generate_db_password()
    username_ciphertext = encrypt(account_name)
    password_ciphertext = encrypt(password)
    resource = DBInstanceResource(
        owner_agent_id=agent_id,
        backend_id=backend_id,
        client_token=client_token,
        request_fingerprint=fingerprint,
        fingerprint_version=_FINGERPRINT_VERSION,
        name=normalized_name,
        engine=engine,
        provisioning_mode=provisioning_mode,
        tenant_name=tenant_name,
        resource_config_name=resource_config_name,
        database_name=database_name,
        permission_snapshot_json=permission_snapshot_to_json(
            default_permission_snapshot(PermissionScope.MULTITENANT)
        ),
    )
    resource.credentials.append(
        InstanceCredential(
            name="resource-access",
            purpose=CredentialPurpose.RESOURCE_ACCESS,
            capability=CredentialCapability.READWRITE,
            username_ciphertext=username_ciphertext,
            password_ciphertext=password_ciphertext,
            database_name=database_name,
            version=1,
        )
    )
    return resource


def _build_dedicated_resource(
    *,
    agent_id: str,
    backend_id: str,
    client_token: str,
    fingerprint: str,
    normalized_name: str | None,
    engine: InstanceEngine,
) -> DBInstanceResource:
    return DBInstanceResource(
        owner_agent_id=agent_id,
        backend_id=backend_id,
        client_token=client_token,
        request_fingerprint=fingerprint,
        fingerprint_version=_FINGERPRINT_VERSION,
        name=normalized_name,
        engine=engine,
        provisioning_mode=ProvisioningMode.DEDICATED,
    )


async def _reserve_dedicated_candidate(
    session: AsyncSession,
    *,
    agent_id: str,
    backend_id: str,
    engine: InstanceEngine,
    client_token: str,
    fingerprint: str,
    normalized_name: str | None,
    allow_cold_purchase: bool,
    before_commit: (
        Callable[[AsyncSession, DBInstanceResource], Awaitable[None]] | None
    ),
) -> DBInstanceResource:
    agent = await session.get(Agent, agent_id)
    backend = await session.get(ProvisioningBackend, backend_id)
    binding = await session.scalar(
        select(AgentProvisioningBinding.id).where(
            AgentProvisioningBinding.agent_id == agent_id,
            AgentProvisioningBinding.backend_id == backend_id,
            AgentProvisioningBinding.enabled.is_(True),
        )
    )
    if (
        agent is None
        or agent.status != AgentStatus.ACTIVE
        or backend is None
        or backend.backend_type != ProvisioningBackendType.DEDICATED_POOL
        or backend.status != ProvisioningBackendStatus.ACTIVE
        or not backend_is_fresh_and_healthy(backend)
        or binding is None
        or backend.dedicated_pool is None
        or backend.dedicated_pool.status != DedicatedPoolStatus.ACTIVE
        or engine != InstanceEngine.POLARDB_MYSQL
    ):
        raise CapacityUnavailable
    pool = backend.dedicated_pool
    await consume_agent_operation_budget(
        session,
        pool=pool,
        agent_id=agent_id,
        operation="create",
        now=utc_now(),
    )
    default_agent_limit = (
        get_config()
        .polardb.tenant_provisioning.effective_max_active_resources_per_agent
    )
    await reserve_active_capacity_counters(
        session,
        agent_id=agent_id,
        backend_id=backend_id,
        agent_limit=agent.max_active_resources or default_agent_limit,
        backend_limit=backend.max_active_resources,
    )
    resource = _build_dedicated_resource(
        agent_id=agent_id,
        backend_id=backend_id,
        client_token=client_token,
        fingerprint=fingerprint,
        normalized_name=normalized_name,
        engine=engine,
    )
    session.add(resource)
    await session.flush()
    member = await claim_fresh_member(
        session,
        pool_id=pool.id,
        resource_id=resource.id,
        now=utc_now(),
    )
    if member is not None:
        activate_dedicated_member(member, resource)
    elif not allow_cold_purchase:
        raise CapacityUnavailable
    elif await reserve_member_purchase(
        session,
        pool_id=pool.id,
        now=utc_now(),
        intent="request",
    ):
        token = secrets.token_hex(16)
        instance = Instance(
            cluster_id=f"pending-dedicated-{token}",
            name=f"Dedicated request member {token[:8]}",
            topology=InstanceTopology.SINGLE_TENANT,
            allocation_mode=AllocationMode.DEDICATED_POOL,
            status=InstanceStatus.CREATING,
            region=pool.region_id,
        )
        session.add(instance)
        await session.flush()
        session.add(
            DedicatedPoolMember(
                pool_id=pool.id,
                instance_id=instance.id,
                status=DedicatedMemberStatus.ALLOCATED_PREPARING,
                readiness_status=ReadinessStatus.STALE,
                preparation_step=DedicatedPreparationStep.PENDING,
                allocated_resource_id=resource.id,
            )
        )
        resource.allocated_instance_id = instance.id
    if before_commit is not None:
        await before_commit(session, resource)
    await session.commit()
    return resource


async def _reserve_dedicated_capacity_and_insert(
    session: AsyncSession,
    *,
    agent_id: str,
    engine: InstanceEngine,
    candidate_ids: list[str],
    client_token: str,
    fingerprint: str,
    normalized_name: str | None,
    before_commit: (
        Callable[[AsyncSession, DBInstanceResource], Awaitable[None]] | None
    ),
) -> DBInstanceResource:
    async with serialized_resource_write(session):
        for allow_cold_purchase in (False, True):
            for backend_id in candidate_ids:
                try:
                    return await _reserve_dedicated_candidate(
                        session,
                        agent_id=agent_id,
                        backend_id=backend_id,
                        engine=engine,
                        client_token=client_token,
                        fingerprint=fingerprint,
                        normalized_name=normalized_name,
                        allow_cold_purchase=allow_cold_purchase,
                        before_commit=before_commit,
                    )
                except CapacityUnavailable:
                    await session.rollback()
                    if session.get_bind().dialect.name == "sqlite":
                        await session.execute(text("BEGIN IMMEDIATE"))
                except (
                    PoolCapacityLimitReached,
                    PurchaseProfileUpgradeRequired,
                    RateLimited,
                ):
                    await session.rollback()
                    if session.get_bind().dialect.name == "sqlite":
                        await session.execute(text("BEGIN IMMEDIATE"))
        raise CapacityUnavailable


async def create_db_instance_resource(
    session: AsyncSession,
    *,
    agent_id: str,
    client_token: str,
    name: str | None,
    db_type: str,
    provisioning_mode: ProvisioningMode = ProvisioningMode.MULTITENANT,
    before_commit: (
        Callable[[AsyncSession, DBInstanceResource], Awaitable[None]] | None
    ) = None,
) -> DBInstanceResource:
    require_idle_resource_session(session)
    validate_client_token(client_token)
    engine = _parse_engine(db_type)
    normalized_name = normalize_resource_name(name)
    fingerprint = request_fingerprint(
        db_type,
        normalized_name,
        provisioning_mode,
        version=_FINGERPRINT_VERSION,
    )

    existing = await _find_by_client_token(session, agent_id, client_token)
    if existing is not None:
        existing_fingerprint = request_fingerprint(
            db_type,
            normalized_name,
            provisioning_mode,
            version=existing.fingerprint_version,
        )
        _return_if_same_request(
            existing,
            fingerprint=existing_fingerprint,
            fingerprint_version=existing.fingerprint_version,
        )
        if before_commit is None:
            return existing
        return await _commit_idempotent_replay(
            session,
            agent_id=agent_id,
            client_token=client_token,
            fingerprint=existing_fingerprint,
            fingerprint_version=existing.fingerprint_version,
            before_commit=before_commit,
        )

    candidates = await list_candidates(
        session,
        agent_id,
        engine,
        client_token,
        mode=provisioning_mode,
    )
    candidate_ids = [candidate.backend_id for candidate in candidates]
    if not candidate_ids:
        await session.rollback()
        raise NoProvisioningBackend(
            "Agent has no active, healthy provisioning backend"
        )
    if session.in_transaction():
        await session.rollback()

    for attempt in range(3):
        try:
            if provisioning_mode == ProvisioningMode.DEDICATED:
                return await _reserve_dedicated_capacity_and_insert(
                    session,
                    agent_id=agent_id,
                    engine=engine,
                    candidate_ids=candidate_ids,
                    client_token=client_token,
                    fingerprint=fingerprint,
                    normalized_name=normalized_name,
                    before_commit=before_commit,
                )
            return await reserve_capacity_and_insert(
                session,
                agent_id=agent_id,
                engine=engine,
                candidate_ids=candidate_ids,
                build_resource=lambda backend_id: _build_resource(
                    agent_id=agent_id,
                    backend_id=backend_id,
                    client_token=client_token,
                    fingerprint=fingerprint,
                    normalized_name=normalized_name,
                    engine=engine,
                    provisioning_mode=provisioning_mode,
                ),
                before_commit=before_commit,
            )
        except CapacityUnavailable as error:
            await session.rollback()
            winner = await _find_by_client_token(
                session,
                agent_id,
                client_token,
            )
            if winner is not None:
                _return_if_same_request(
                    winner,
                    fingerprint=fingerprint,
                    fingerprint_version=_FINGERPRINT_VERSION,
                )
                if before_commit is None:
                    return winner
                return await _commit_idempotent_replay(
                    session,
                    agent_id=agent_id,
                    client_token=client_token,
                    fingerprint=fingerprint,
                    fingerprint_version=_FINGERPRINT_VERSION,
                    before_commit=before_commit,
                )
            raise CapacityExhausted(
                "Provisioning capacity is exhausted"
            ) from error
        except IntegrityError:
            await session.rollback()
            winner = await _find_by_client_token(
                session,
                agent_id,
                client_token,
            )
            if winner is not None:
                _return_if_same_request(
                    winner,
                    fingerprint=fingerprint,
                    fingerprint_version=_FINGERPRINT_VERSION,
                )
                if before_commit is None:
                    return winner
                return await _commit_idempotent_replay(
                    session,
                    agent_id=agent_id,
                    client_token=client_token,
                    fingerprint=fingerprint,
                    fingerprint_version=_FINGERPRINT_VERSION,
                    before_commit=before_commit,
                )
            if attempt == 2:
                raise
            await session.rollback()
        except Exception:
            await session.rollback()
            raise
    raise RuntimeError("unreachable")


def _serialized_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


async def describe_db_instance_resource(
    session: AsyncSession,
    principal: Principal,
    db_instance_id: str,
) -> dict:
    if principal.kind != PrincipalKind.AGENT:
        raise DBInstanceNotFound("Database instance not found")
    row = (
        await session.execute(
            select(DBInstanceResource, Instance)
            .join(
                ProvisioningBackend,
                ProvisioningBackend.id == DBInstanceResource.backend_id,
            )
            .outerjoin(
                Instance,
                Instance.id
                == func.coalesce(
                    DBInstanceResource.allocated_instance_id,
                    ProvisioningBackend.instance_id,
                ),
            )
            .join(Agent, Agent.id == DBInstanceResource.owner_agent_id)
            .where(
                DBInstanceResource.id == db_instance_id,
                DBInstanceResource.owner_agent_id == principal.id,
                Agent.status == AgentStatus.ACTIVE,
            )
        )
    ).one_or_none()
    if row is None:
        raise DBInstanceNotFound("Database instance not found")
    resource, instance = row
    if resource.status == DBInstanceStatus.READY and instance is None:
        raise DBInstanceNotFound("Database instance not found")
    connection = (
        resource_connection_details(resource)
        if resource.status == DBInstanceStatus.READY
        else None
    )
    payload: dict = {
        "db_instance_id": resource.id,
        "name": resource.name,
        "usage": None,
        "db_type": resource.engine.value,
        "source": "provisioned",
        "status": resource.status.value.upper(),
        "capabilities": list(
            resource_capabilities(
                resource,
                credentials_revealable=connection is not None,
            )
        ),
        "created_at": _serialized_timestamp(resource.created_at),
        "updated_at": _serialized_timestamp(resource.updated_at),
        "provisioning_step": resource.provisioning_step.value.upper(),
        "cleanup_step": resource.cleanup_step.value.upper(),
        "cleanup_required": resource.cleanup_required,
        "delete_step": resource.delete_step.value.upper(),
        "delete_requested_at": _serialized_timestamp(
            resource.delete_requested_at
        ),
        "disconnected_at": _serialized_timestamp(resource.disconnected_at),
        "cooldown_until": _serialized_timestamp(resource.cooldown_until),
        "delete_cooldown_duration_hours": (
            resource.effective_delete_cooldown_duration_hours
        ),
        "reclaim_policy": (
            resource.reclaim_policy.value
            if resource.reclaim_policy is not None
            else None
        ),
    }
    if connection is not None:
        payload.update(
            {
                "host": instance.host,
                "port": instance.port,
                "database": connection.database,
                "username": connection.username,
                "password": connection.password,
            }
        )
    if resource.failure_reason and resource.status in {
        DBInstanceStatus.FAILED,
        DBInstanceStatus.DELETE_FAILED,
    }:
        payload["failure_reason"] = resource.failure_reason
    if resource.restore_failure_reason:
        payload["restore_failure_reason"] = resource.restore_failure_reason
    return payload


async def delete_db_instance_resource(
    session: AsyncSession,
    agent_id: str,
    db_instance_id: str,
    before_commit: (
        Callable[[AsyncSession, DBInstanceResource], Awaitable[None]] | None
    ) = None,
) -> DBInstanceResource:
    async with serialized_resource_write(session):
        statement = select(DBInstanceResource).where(
            DBInstanceResource.id == db_instance_id,
            DBInstanceResource.owner_agent_id == agent_id,
        )
        if session.get_bind().dialect.name != "sqlite":
            statement = statement.with_for_update()
        resource = (await session.execute(statement)).scalar_one_or_none()
        if resource is None:
            raise DBInstanceNotFound("Database instance not found")
        if resource.status in {
            DBInstanceStatus.CREATING,
            DBInstanceStatus.READY,
            DBInstanceStatus.FAILED,
            DBInstanceStatus.DELETE_FAILED,
        }:
            previous_status = resource.status
            backend = await session.get(
                ProvisioningBackend, resource.backend_id
            )
            if backend is None:
                raise NoProvisioningBackend(
                    "Provisioning backend is unavailable"
                )
            if resource.delete_requested_at is None:
                resource.delete_requested_at = utc_now()
                pool = backend.dedicated_pool
                resource.effective_delete_cooldown_duration_hours = (
                    effective_delete_cooldown_hours(
                        resource_override=(
                            resource.effective_delete_cooldown_duration_hours
                        ),
                        pool_or_backend_override=(
                            pool.delete_cooldown_duration_hours
                            if pool is not None
                            else backend.delete_cooldown_duration_hours
                        ),
                        global_default=(
                            get_config()
                            .polardb.tenant_provisioning
                            .delete_cooldown_duration_hours
                        ),
                    )
                )
                resource.reclaim_policy = (
                    pool.reclaim_policy
                    if pool is not None
                    else ReclaimPolicy.DESTROY
                )
            for credential in resource.credentials:
                if credential.purpose == CredentialPurpose.RESOURCE_ACCESS:
                    credential.status = CredentialStatus.REVOKED
            if resource.provisioning_mode == ProvisioningMode.DEDICATED:
                member = await session.scalar(
                    select(DedicatedPoolMember).where(
                        DedicatedPoolMember.allocated_resource_id
                        == resource.id
                    )
                )
                if member is not None:
                    member.status = DedicatedMemberStatus.DELETING
                    try:
                        await consume_agent_operation_budget(
                            session,
                            pool=member.pool,
                            agent_id=agent_id,
                            operation="delete",
                            now=utc_now(),
                        )
                    except RateLimited:
                        # Revocation and disconnection are safety operations;
                        # throttling must never leave access active.
                        pass
            live_claim = False
            if resource.worker_id and resource.worker_lease_until is not None:
                lease_until = resource.worker_lease_until
                if lease_until.tzinfo is None:
                    lease_until = lease_until.replace(tzinfo=timezone.utc)
                live_claim = lease_until > utc_now()
            resource.status = DBInstanceStatus.DELETING
            resource.cleanup_required = True
            resource.retry_count = 0
            resource.next_retry_at = None
            resource.failure_reason = None
            if (
                not live_claim
                or previous_status == DBInstanceStatus.DELETE_FAILED
            ):
                resource.worker_id = None
                resource.worker_lease_until = None
        await session.flush()
        if before_commit is not None:
            await before_commit(session, resource)
        await session.commit()
        return resource


async def restore_db_instance_resource(
    session: AsyncSession,
    db_instance_id: str,
) -> DBInstanceResource:
    async with serialized_resource_write(session):
        statement = select(DBInstanceResource).where(
            DBInstanceResource.id == db_instance_id
        )
        if session.get_bind().dialect.name != "sqlite":
            statement = statement.with_for_update()
        resource = (await session.execute(statement)).scalar_one_or_none()
        if resource is None:
            raise DBInstanceNotFound("Database instance not found")
        if resource.status not in {
            DBInstanceStatus.DELETING,
            DBInstanceStatus.DELETE_FAILED,
            DBInstanceStatus.COOLING_DOWN,
        }:
            raise DBInstanceServiceError(
                "Database instance cannot be restored from its current state"
            )
        if resource.delete_step in {
            DeleteLifecycleStep.LOGICAL_CLEANUP,
            DeleteLifecycleStep.PHYSICAL_DESTROY,
            DeleteLifecycleStep.SANITIZE_DISPATCH,
            DeleteLifecycleStep.COMPLETE,
        }:
            raise DBInstanceServiceError(
                "Database cleanup has crossed the irreversible boundary"
            )
        resource.restore_source_status = resource.status
        resource.status = DBInstanceStatus.RESTORING
        resource.restore_failure_reason = None
        resource.worker_id = None
        resource.worker_lease_until = None
        resource.next_retry_at = None
        resource.retry_count = 0
        await session.commit()
        return resource
