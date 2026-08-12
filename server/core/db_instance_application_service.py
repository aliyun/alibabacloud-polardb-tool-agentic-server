from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.principal import Principal, PrincipalKind
from server.core.db_instance_contract import DBInstanceConnectionView
from server.core.db_instance_metrics import emit_dedicated_pool_signal
from server.core.db_instance_service import (
    DBInstanceNotFound,
    create_db_instance_resource,
    delete_db_instance_resource,
    describe_db_instance_resource,
)
from server.models import DBInstanceResource, ProvisioningMode

CommitCallback = Callable[
    [AsyncSession, DBInstanceResource], Awaitable[None]
]


@dataclass(frozen=True, slots=True)
class CreateDBInstanceCommand:
    agent_id: str
    mode: ProvisioningMode
    idempotency_key: str
    name: str | None
    db_type: str


@dataclass(frozen=True, slots=True)
class DBInstanceView:
    resource_id: str
    name: str | None
    db_type: str
    source: str
    status: str
    provisioning_mode: ProvisioningMode
    connection: DBInstanceConnectionView | None = None
    failure_reason: str | None = None
    metadata: dict[str, Any] | None = None

    @classmethod
    def from_resource(cls, resource: DBInstanceResource) -> "DBInstanceView":
        return cls(
            resource_id=resource.id,
            name=resource.name,
            db_type=resource.engine.value,
            source="provisioned",
            status=resource.status.value.upper(),
            provisioning_mode=resource.provisioning_mode,
            failure_reason=resource.failure_reason,
        )

    @classmethod
    def from_describe_payload(
        cls,
        payload: dict[str, Any],
        mode: ProvisioningMode,
    ) -> "DBInstanceView":
        connection = None
        connection_keys = {
            "host",
            "port",
            "database",
            "username",
            "password",
        }
        if connection_keys <= payload.keys():
            connection = DBInstanceConnectionView(
                host=payload["host"],
                port=payload["port"],
                database=payload["database"],
                username=payload["username"],
                password=payload["password"],
            )
        return cls(
            resource_id=payload["db_instance_id"],
            name=payload["name"],
            db_type=payload["db_type"],
            source=payload["source"],
            status=payload["status"],
            provisioning_mode=mode,
            connection=connection,
            failure_reason=payload.get("failure_reason"),
            metadata={
                key: value
                for key, value in payload.items()
                if key not in connection_keys
            },
        )

    def summary_payload(self) -> dict[str, Any]:
        payload = {
            "db_instance_id": self.resource_id,
            "name": self.name,
            "db_type": self.db_type,
            "source": self.source,
            "status": self.status,
            "provisioning_mode": self.provisioning_mode.value,
        }
        if self.failure_reason is not None:
            payload["failure_reason"] = self.failure_reason
        return payload

    def describe_payload(self) -> dict[str, Any]:
        payload = (
            {
                **self.metadata,
                "provisioning_mode": self.provisioning_mode.value,
            }
            if self.metadata is not None
            else self.summary_payload()
        )
        if self.connection is not None:
            payload.update(
                {
                    "host": self.connection.host,
                    "port": self.connection.port,
                    "database": self.connection.database,
                    "username": self.connection.username,
                    "password": self.connection.password,
                }
            )
        if self.failure_reason is not None:
            payload["failure_reason"] = self.failure_reason
        return payload


class DBInstanceApplicationService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        create_before_commit: CommitCallback | None = None,
        delete_before_commit: CommitCallback | None = None,
    ) -> None:
        self._session = session
        self._create_before_commit = create_before_commit
        self._delete_before_commit = delete_before_commit

    async def create(
        self,
        command: CreateDBInstanceCommand,
    ) -> DBInstanceView:
        started_at = time.perf_counter()
        try:
            resource = await create_db_instance_resource(
                self._session,
                agent_id=command.agent_id,
                client_token=command.idempotency_key,
                name=command.name,
                db_type=command.db_type,
                provisioning_mode=command.mode,
                before_commit=self._create_before_commit,
            )
        except Exception:
            if command.mode == ProvisioningMode.DEDICATED:
                emit_dedicated_pool_signal(
                    signal="allocation",
                    outcome="failed",
                    duration_seconds=time.perf_counter() - started_at,
                )
            raise
        if command.mode == ProvisioningMode.DEDICATED:
            emit_dedicated_pool_signal(
                signal="allocation",
                outcome=(
                    "hot_hit"
                    if resource.status.value == "ready"
                    else "cold_miss"
                ),
                duration_seconds=time.perf_counter() - started_at,
            )
        if resource.status.value == "ready":
            payload = await describe_db_instance_resource(
                self._session,
                Principal(PrincipalKind.AGENT, command.agent_id),
                resource.id,
            )
            view = DBInstanceView.from_describe_payload(
                payload,
                resource.provisioning_mode,
            )
        else:
            view = DBInstanceView.from_resource(resource)
        if self._session.in_transaction():
            await self._session.rollback()
        return view

    async def get_resource(
        self,
        *,
        agent_id: str,
        resource_id: str,
    ) -> DBInstanceResource:
        resource = (
            await self._session.execute(
                select(DBInstanceResource).where(
                    DBInstanceResource.id == resource_id,
                    DBInstanceResource.owner_agent_id == agent_id,
                )
            )
        ).scalar_one_or_none()
        if resource is None:
            raise DBInstanceNotFound("Database instance not found")
        return resource

    async def describe(
        self,
        *,
        agent_id: str,
        resource_id: str,
    ) -> DBInstanceView:
        resource = await self.get_resource(
            agent_id=agent_id,
            resource_id=resource_id,
        )
        payload = await describe_db_instance_resource(
            self._session,
            Principal(PrincipalKind.AGENT, agent_id),
            resource_id,
        )
        return DBInstanceView.from_describe_payload(
            payload,
            resource.provisioning_mode,
        )

    async def delete(
        self,
        *,
        agent_id: str,
        resource_id: str,
    ) -> DBInstanceView:
        resource = await delete_db_instance_resource(
            self._session,
            agent_id,
            resource_id,
            before_commit=self._delete_before_commit,
        )
        if resource.provisioning_mode == ProvisioningMode.DEDICATED:
            emit_dedicated_pool_signal(
                signal="lifecycle", outcome="delete_requested"
            )
        return DBInstanceView.from_resource(resource)
