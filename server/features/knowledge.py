from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from server.configuration.repository import ConfigConflict, ConfigRepository
from server.configuration.types import ConfigError, ModuleDocument
from server.models import SystemConfig

GATE_KEY = "feature.knowledge.admission"
REPLICA_PREFIX = "feature.knowledge.replica."
HEARTBEAT_SECONDS = 5
STALE_SECONDS = 30
_runtime: KnowledgeRuntime | None = None
_operation = ContextVar("knowledge_operation", default=None)


class _StateConflict(Exception):
    pass


class KnowledgeUnavailable(ConfigError):
    def __init__(self, code="KNOWLEDGE_NOT_ENABLED"):
        super().__init__(code, "Knowledge is unavailable. Check Settings → Features for activation or drain status.")


def runtime() -> KnowledgeRuntime | None:
    return _runtime


def install_runtime(value: KnowledgeRuntime | None) -> None:
    global _runtime
    _runtime = value


def loaded() -> bool:
    # Embedders without an application lifecycle retain the legacy behavior.
    return _runtime.loaded_enabled if _runtime is not None else True


class KnowledgeState:
    """Short CAS transactions; never hold a DB transaction across upstream I/O.

    An interrupted operation remains recorded, blocking disable until reconciled.
    No time-based expiry may turn an uncertain external write into success.
    """

    def __init__(self, repository: ConfigRepository):
        self.repository = repository

    async def mutate(self, key, callback):
        for attempt in range(8):
            try:
                async with self.repository.session_factory() as session:
                    async with session.begin():
                        row = await session.get(SystemConfig, key, with_for_update=True)
                        value = json.loads(row.config_value) if row is not None else {}
                        result = await callback(value, session)
                        serialized = json.dumps(value, sort_keys=True, separators=(",", ":"))
                        if row is None:
                            session.add(SystemConfig(config_key=key, config_value=serialized, config_version=1))
                            await session.flush()
                        else:
                            changed = await session.execute(
                                update(SystemConfig)
                                .where(
                                    SystemConfig.config_key == key,
                                    SystemConfig.config_version == row.config_version,
                                )
                                .values(config_value=serialized, config_version=row.config_version + 1)
                            )
                            if changed.rowcount != 1:
                                raise _StateConflict("knowledge state changed concurrently")
                    return result
            except (IntegrityError, _StateConflict, OperationalError):
                if attempt == 7:
                    raise KnowledgeUnavailable("KNOWLEDGE_STATE_BUSY") from None
                await asyncio.sleep(0.01 * (attempt + 1))

    async def read(self, key):
        row = await self.repository.get_config_row(key)
        return json.loads(row.config_value) if row else {}

    async def desired(self, session=None):
        if session is None:
            document = await self.repository.get_module("knowledge")
        else:
            row = await session.get(SystemConfig, "module.knowledge")
            document = ModuleDocument.model_validate_json(row.config_value) if row else None
        if document is None or document.effective is None:
            raise KnowledgeUnavailable("KNOWLEDGE_CONFIG_UNAVAILABLE")
        return document.effective

    async def initialize(self):
        desired = await self.desired()

        async def initialize(value, _session):
            if not value:
                legacy = desired.config.get("enabled") and not desired.config.get("validation_resource_id")
                value.update(active_revision=desired.revision if legacy else None, draining=False, operations={})

        await self.mutate(GATE_KEY, initialize)

    async def begin_drain(self):
        await self.initialize()

        async def drain(value, _session):
            value["draining"] = True

        await self.mutate(GATE_KEY, drain)

    async def cancel_drain(self, expected_revision: int):
        async def cancel(value, session):
            desired = await self.desired(session)
            if desired.revision != expected_revision:
                raise ConfigError("REVISION_CONFLICT", "Knowledge configuration changed")
            value["draining"] = False

        await self.mutate(GATE_KEY, cancel)

    async def disable_blockers(self):
        from server.models import PolarRAGSpace, PolarRAGUploadCleanup, PolarRAGUploadSession, PolarRAGUploadStatus

        state = await self.read(GATE_KEY)
        async with self.repository.session_factory() as session:
            syncing = await session.scalar(
                select(func.count()).select_from(PolarRAGSpace).where(PolarRAGSpace.catalog_sync_status == "running")
            )
            pending = await session.scalar(select(func.count()).select_from(PolarRAGUploadCleanup))
            uploads = await session.scalar(
                select(func.count())
                .select_from(PolarRAGUploadSession)
                .where(PolarRAGUploadSession.status.in_([PolarRAGUploadStatus.PREPARED, PolarRAGUploadStatus.UPLOADED]))
            )
        return {
            "in_flight": len(state.get("operations", {})),
            "pending_cleanup": pending,
            "pending_uploads": uploads,
            "catalog_syncs": syncing,
        }

    async def replicas(self):
        async with self.repository.session_factory() as session:
            rows = (
                await session.scalars(select(SystemConfig).where(SystemConfig.config_key.startswith(REPLICA_PREFIX)))
            ).all()
        return [
            json.loads(row.config_value)
            for row in rows
            if json.loads(row.config_value).get("heartbeat", 0) > time.time() - STALE_SECONDS
        ]

    async def confirm_activation(self, revision: int, replica_ids: list[str]):
        live = await self.replicas()
        if not replica_ids or len(replica_ids) != len(set(replica_ids)) or set(replica_ids) != {r["id"] for r in live}:
            raise ConfigError(
                "KNOWLEDGE_REPLICAS_NOT_READY", "Supply every live target replica after completing the rolling restart"
            )
        if any(
            r["loaded_revision"] != revision or not r["prepared"] or not r["loaded_enabled"] or r["error_code"]
            for r in live
        ):
            raise ConfigError(
                "KNOWLEDGE_REPLICAS_NOT_READY", "Every replica must load and validate the same knowledge revision"
            )

        async def activate(value, session):
            desired = await self.desired(session)
            if desired.revision != revision or not desired.config.get("enabled"):
                raise ConfigError("REVISION_CONFLICT", "Knowledge configuration changed")
            if value.get("operations"):
                raise ConfigError("KNOWLEDGE_DRAINING", "Wait for existing operations to finish")
            value.update(active_revision=revision, draining=False)

        await self.mutate(GATE_KEY, activate)


class KnowledgeRuntime:
    def __init__(self, state: KnowledgeState, *, managed: bool, replica_id: str | None = None):
        self.state = state
        self.managed = managed
        self.replica_id = replica_id or os.environ.get("PAS_RUNTIME_ID") or str(uuid4())
        self.loaded_revision = 0
        self.loaded_enabled = False
        self.error_code = None
        self.prepared = False

    async def start(self):
        await self.state.initialize()
        desired = await self.state.desired()
        self.loaded_revision = desired.revision
        self.loaded_enabled = desired.config.get("enabled") is True
        if self.loaded_enabled and desired.config.get("validation_resource_id"):
            try:
                await validate_read_access(self.state.repository, desired.config)
            except ConfigError as error:
                self.error_code = error.code
                self.loaded_enabled = False
        self.prepared = self.error_code is None
        await self.heartbeat()

    async def heartbeat(self, *, stopped=False):
        async def beat(value, _session):
            value.update(
                id=self.replica_id,
                loaded_revision=self.loaded_revision,
                loaded_enabled=self.loaded_enabled,
                prepared=self.prepared,
                error_code=self.error_code,
                heartbeat=0 if stopped else time.time(),
            )

        await self.state.mutate(REPLICA_PREFIX + self.replica_id, beat)
        if not stopped and not self.managed and self.loaded_enabled and self.prepared:
            desired = await self.state.desired()
            gate = await self.state.read(GATE_KEY)
            if desired.revision == self.loaded_revision and gate.get("active_revision") != self.loaded_revision:
                replicas = await self.state.replicas()
                if len(replicas) == 1 and replicas[0]["id"] == self.replica_id:
                    await self.state.confirm_activation(self.loaded_revision, [self.replica_id])

    async def poll(self):
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            try:
                await self.heartbeat()
            except Exception:
                # The request gate reads the DB directly and fails closed.
                import logging

                logging.getLogger(__name__).exception("Knowledge heartbeat failed")

    async def available(self):
        if not self.loaded_enabled or self.error_code:
            return False
        desired = await self.state.desired()
        gate = await self.state.read(GATE_KEY)
        return bool(
            desired.config.get("enabled")
            and desired.revision == self.loaded_revision
            and gate.get("active_revision") == self.loaded_revision
            and not gate.get("draining")
        )

    @asynccontextmanager
    async def operation(self, kind: str, *, cleanup=False):
        if _operation.get() is not None:
            yield
            return
        identity = str(uuid4())

        async def enter(value, session):
            desired = await self.state.desired(session)
            if (
                not self.loaded_enabled
                or self.error_code
                or not desired.config.get("enabled")
                or desired.revision != self.loaded_revision
            ):
                raise KnowledgeUnavailable()
            if not cleanup and (value.get("draining") or value.get("active_revision") != self.loaded_revision):
                raise KnowledgeUnavailable("KNOWLEDGE_ACTIVATING_OR_DRAINING")
            operations = value.setdefault("operations", {})
            if len(operations) >= 256:
                raise KnowledgeUnavailable("KNOWLEDGE_TOO_MANY_OPERATIONS")
            operations[identity] = {"replica_id": self.replica_id, "kind": kind, "started_at": time.time()}

        await self.state.mutate(GATE_KEY, enter)
        token = _operation.set(identity)
        interrupted = False
        try:
            yield
        except asyncio.CancelledError:
            interrupted = True
            raise
        finally:
            _operation.reset(token)

            async def leave(value, _session):
                value.get("operations", {}).pop(identity, None)

            if not interrupted:
                await asyncio.shield(self.state.mutate(GATE_KEY, leave))

    async def status(self):
        desired = await self.state.desired()
        gate = await self.state.read(GATE_KEY)
        active = await self.available()
        state = (
            "FAILED"
            if self.error_code and desired.revision == self.loaded_revision
            else "DRAINING"
            if gate.get("draining") and desired.config.get("enabled")
            else "PENDING_RESTART"
            if desired.revision != self.loaded_revision
            else "DISABLED"
            if not desired.config.get("enabled") and not self.loaded_enabled
            else "DRAINING"
            if gate.get("draining")
            else "ENABLED"
            if active
            else "ACTIVATING"
            if desired.config.get("enabled")
            else "DISABLED"
        )
        return {
            "state": state,
            "available": active,
            "managed": self.managed,
            "desired_enabled": desired.config.get("enabled") is True,
            "desired_revision": desired.revision,
            "loaded_revision": self.loaded_revision,
            "loaded_enabled": self.loaded_enabled,
            "prepared": self.prepared,
            "error_code": self.error_code,
            "replica_id": self.replica_id,
            "replicas": await self.state.replicas(),
            "blockers": await self.state.disable_blockers(),
        }


async def validate_read_access(repository: ConfigRepository, config: dict):
    if not config.get("validation_resource_id") or not config.get("validation_user_id"):
        raise ConfigError(
            "KNOWLEDGE_READ_TEST_REQUIRED",
            "Select a knowledge resource and PAS user and verify read access before enabling",
        )
    from server.models import User, UserStatus
    from server.polarrag.access import KnowledgeAccessError, plan_knowledge_access
    from server.polarrag.client import client_from_instance
    from server.polarrag.contracts import PolarRAGUpstreamError

    async with repository.session_factory() as session:
        user = await session.get(User, config["validation_user_id"])
        if user is None or user.status != UserStatus.ACTIVE:
            raise ConfigError("KNOWLEDGE_READ_TEST_REQUIRED", "Select an active PAS user")
        try:
            plan = await plan_knowledge_access(session, user, [config["validation_resource_id"]])
            capabilities = await client_from_instance(plan.instance).check_capabilities()
            if not all(value for key, value in capabilities.as_dict().items() if key != "version"):
                raise ConfigError(
                    "KNOWLEDGE_CAPABILITY_MISSING", "The upstream service lacks required protected knowledge APIs"
                )
            await client_from_instance(plan.instance).search(
                plan.space.space_id,
                plan.resources[0].kb_id,
                query="PAS knowledge read access verification",
                search_mode="fulltext",
                top_k=1,
                min_score=None,
                reranker=False,
                acl_context=plan.acl_context,
            )
        except KnowledgeAccessError as error:
            raise ConfigError(
                "KNOWLEDGE_READ_FORBIDDEN", "The selected PAS identity cannot access this knowledge resource"
            ) from error
        except PolarRAGUpstreamError as error:
            raise ConfigError(error.code.value, "Knowledge connection or protected read verification failed") from error


async def activate_configuration(repository: ConfigRepository, *, expected_revision: int, document: ModuleDocument):
    state = KnowledgeState(repository)
    await state.initialize()
    enabled = document.effective.config.get("enabled") is True
    current = await state.desired()
    if enabled:
        if not current.config.get("enabled") or document.effective.config.get("validation_resource_id"):
            await validate_read_access(repository, document.effective.config)
    else:
        await state.begin_drain()

    async def activate(gate, session):
        if gate.get("operations"):
            raise ConfigError("KNOWLEDGE_DRAINING", "Existing knowledge operations must finish before activation")
        if not enabled:
            from server.models import PolarRAGSpace, PolarRAGUploadCleanup, PolarRAGUploadSession, PolarRAGUploadStatus

            syncing = await session.scalar(
                select(func.count()).select_from(PolarRAGSpace).where(PolarRAGSpace.catalog_sync_status == "running")
            )
            pending = await session.scalar(select(func.count()).select_from(PolarRAGUploadCleanup))
            uploads = await session.scalar(
                select(func.count())
                .select_from(PolarRAGUploadSession)
                .where(PolarRAGUploadSession.status.in_([PolarRAGUploadStatus.PREPARED, PolarRAGUploadStatus.UPLOADED]))
            )
            if pending or uploads or syncing:
                raise ConfigError(
                    "KNOWLEDGE_CLEANUP_PENDING",
                    "Keep PAS running until uploads expire and cleanup completes; resolve failed external operations before disabling",
                )
        await repository.compare_and_set_module_in_session(
            session, "knowledge", expected_revision=expected_revision, document=document
        )

    try:
        await state.mutate(GATE_KEY, activate)
    except ConfigConflict as error:
        raise ConfigError("REVISION_CONFLICT", "Knowledge configuration changed") from error
