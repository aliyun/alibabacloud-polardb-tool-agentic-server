"""Durable, Agent-scoped permission refresh with atomic per-KB publication."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from datetime import UTC, datetime, timedelta
import hashlib
import json
import logging
import uuid
import time
from weakref import WeakKeyDictionary, ref

from fastapi import HTTPException
from sqlalchemy import delete, select

from server.core.crypto import decrypt
from server.core.external_knowledge_scope_sync import (
    ImportedGrant,
    ImportedSnapshot,
    ExternalKnowledgeScopeSyncError,
    import_external_knowledge_scope_snapshot,
    lock_agent_knowledge_configuration,
)
from server.core.agent_knowledge_scope import resolve_agent_global_resource_scope
from server.db.engine import get_session_factory
from server.enterprise_identity.acl_snapshot import (
    FeishuAclMembershipSnapshotClient,
    acl_snapshot_client_for,
    AclMembershipSnapshotCapacityError,
)
from server.enterprise_identity.etl_permissions import write
from server.enterprise_identity.providers import acl_membership_snapshot_config
from server.enterprise_identity.service import identity_source_snapshot_is_usable
from server.models import (
    Agent,
    AgentKnowledgeScopeMode,
    AgentKnowledgeScope,
    AgentKnowledgeScopeBinding,
    AgentKnowledgeScopeOrigin,
    EnterpriseIdentitySource,
    EnterpriseIdentitySourceSpaceBinding,
    ExternalKnowledgeScopeSnapshot,
    ExternalKnowledgeScopeGrant,
    IdentitySourceProvider,
    KnowledgeResource,
    KnowledgeResourceSyncStatus,
    SystemConfig,
)

logger = logging.getLogger(__name__)
PREFIX = "knowledge_permission_refresh.job."
LEASE_SECONDS = 180
KB_TIMEOUT_SECONDS = 120
MAX_GRANTS_PER_KB = 200_000
# Two workers per process/event loop; leases fence publication across processes.
_workers = WeakKeyDictionary()
_started_tasks: set[asyncio.Task] = set()


def _worker_semaphore():
    loop = asyncio.get_running_loop()
    reference = _workers.get(loop)
    semaphore = reference() if reference else None
    if semaphore is None:
        semaphore = asyncio.Semaphore(2)
        _workers[loop] = ref(semaphore)
    return semaphore


class RefreshPreconditionError(HTTPException):
    def __init__(self, status_code, detail, code):
        super().__init__(status_code, detail)
        self.code = code


def _digest(source):
    return hashlib.sha256((source.config_ciphertext or "").encode()).hexdigest()


def _configuration(source):
    if (
        source.provider not in {IdentitySourceProvider.FEISHU, IdentitySourceProvider.DINGTALK}
        or not (source.tenant_id or "").strip()
        or not identity_source_snapshot_is_usable(source)
    ):
        raise RefreshPreconditionError(409, "A usable Feishu identity source is required", "REFRESH_SOURCE_CHANGED")
    try:
        config = acl_membership_snapshot_config(json.loads(decrypt(source.config_ciphertext)))
    except Exception as exc:
        raise RefreshPreconditionError(
            409, "Knowledge permission data source is invalid", "REFRESH_SOURCE_CHANGED"
        ) from exc
    if not config:
        raise RefreshPreconditionError(
            409, "Knowledge permission data source is not configured", "REFRESH_SOURCE_CHANGED"
        )
    return config


def _agent_enabled(agent):
    if agent is None:
        raise HTTPException(404, "Agent not found")
    if agent.knowledge_scope_mode != AgentKnowledgeScopeMode.SCOPED or not agent.external_knowledge_auto_bind_enabled:
        raise RefreshPreconditionError(
            409, "SCOPED with external auto-binding enabled is required", "REFRESH_AGENT_CHANGED"
        )


def public(value):
    return {k: v for k, v in value.items() if k not in {"owner", "lease_until", "targets"}}


async def status(session, agent_id, job_id):
    row = await session.get(SystemConfig, PREFIX + job_id, populate_existing=True)
    if row is None or json.loads(row.config_value)["agent_id"] != agent_id:
        raise HTTPException(404, "Knowledge permission refresh not found")
    return public(json.loads(row.config_value))


async def enqueue(
    session, agent_id, admin_id, *, source_ids=None, resource_ids=None,
    kb_ids=None, space_id=None, polarrag_instance_id=None,
):
    if kb_ids is not None and resource_ids is not None:
        raise HTTPException(422, "Use kb_ids or knowledge_resource_ids, not both")
    if kb_ids is None and (space_id is not None or polarrag_instance_id is not None):
        raise HTTPException(422, "Instance and Space selectors require kb_ids")
    if kb_ids is not None and not kb_ids:
        raise HTTPException(422, "kb_ids must not be empty")
    await lock_agent_knowledge_configuration(session, agent_id)
    agent = await session.get(Agent, agent_id, populate_existing=True)
    _agent_enabled(agent)
    pointer_key = "knowledge_permission_refresh.agent." + agent_id
    pointer = await session.get(SystemConfig, pointer_key, populate_existing=True)
    prior = None
    if pointer:
        prior = await session.get(SystemConfig, PREFIX + json.loads(pointer.config_value)["job_id"])
        if prior and json.loads(prior.config_value)["state"] in {"QUEUED", "RUNNING"}:
            raise HTTPException(
                409, {"code": "REFRESH_ALREADY_RUNNING", "job_id": json.loads(prior.config_value)["job_id"]}
            )
    base = await resolve_agent_global_resource_scope(session, agent_id)
    query = select(KnowledgeResource).where(
        KnowledgeResource.sync_status == KnowledgeResourceSyncStatus.ACTIVE,
        KnowledgeResource.enabled.is_(True),
    )
    if resource_ids is not None:
        query = query.where(KnowledgeResource.id.in_(resource_ids))
    if kb_ids is not None:
        query = query.where(KnowledgeResource.kb_id.in_(kb_ids))
        if space_id is not None:
            query = query.where(KnowledgeResource.space_id == space_id)
        if polarrag_instance_id is not None:
            query = query.where(KnowledgeResource.polarrag_instance_id == polarrag_instance_id)
    resources = [r for r in await session.scalars(query) if base.allows(r)]
    if kb_ids is not None:
        # Resolve before source filtering: an identity source cannot disambiguate resource identity.
        matches_by_kb = {}
        for resource in resources:
            matches_by_kb.setdefault(resource.kb_id, []).append(resource)
        for kb_id in sorted(set(kb_ids)):
            matches = matches_by_kb.get(kb_id, [])
            if not matches:
                raise HTTPException(422, {
                    "code": "KNOWLEDGE_RESOURCE_NOT_FOUND", "kb_id": kb_id,
                    "space_id": space_id, "polarrag_instance_id": polarrag_instance_id,
                    "message": "No active enabled resource matched the applied selectors within the Agent scope",
                })
            if len(matches) != 1:
                raise HTTPException(422, {
                    "code": "KNOWLEDGE_RESOURCE_AMBIGUOUS", "kb_id": kb_id,
                    "message": "Specify polarrag_instance_id and/or space_id to resolve ambiguity",
                })
        resource_ids = [r.id for r in resources]
    if resource_ids is not None and {r.id for r in resources} != set(resource_ids):
        raise HTTPException(422, "Knowledge resource is missing, inactive, or outside Agent global scope")
    sources = list(
        await session.scalars(
            select(EnterpriseIdentitySource).where(
                EnterpriseIdentitySource.provider.in_((IdentitySourceProvider.FEISHU, IdentitySourceProvider.DINGTALK)),
            )
        )
    )
    if source_ids is not None:
        sources = [s for s in sources if s.id in source_ids]
        if {s.id for s in sources} != set(source_ids):
            raise HTTPException(422, "Identity source is missing or unsupported")
    configured = {}
    for source in sources:
        try:
            _configuration(source)
        except HTTPException:
            if source_ids is not None:
                raise
            continue
        configured[source.id] = source
    bindings = list(
        (
            await session.execute(
                select(
                    EnterpriseIdentitySourceSpaceBinding.identity_source_id,
                    EnterpriseIdentitySourceSpaceBinding.knowledge_space_id,
                ).where(EnterpriseIdentitySourceSpaceBinding.identity_source_id.in_(configured))
            )
        ).tuples()
    )
    targets = []
    used_sources = set()
    for resource in sorted(resources, key=lambda r: r.id):
        ids = sorted({sid for sid, space in bindings if space == resource.knowledge_space_id})
        if not ids:
            if resource_ids is not None:
                if kb_ids is not None:
                    raise HTTPException(422, {
                        "code": "KNOWLEDGE_RESOURCE_NO_CONFIGURED_IDENTITY_SOURCE",
                        "kb_id": resource.kb_id,
                        "space_id": resource.space_id,
                        "polarrag_instance_id": resource.polarrag_instance_id,
                        "message": "Knowledge resource has no selected configured identity source bound to its Space",
                    })
                raise HTTPException(422, "Knowledge resource has no selected configured identity source")
            continue
        used_sources.update(ids)
        targets.append(
            {
                "knowledge_resource_id": resource.id,
                "polarrag_instance_id": resource.polarrag_instance_id,
                "space_id": resource.space_id,
                "kb_id": resource.kb_id,
                "sources": [
                    {"id": sid, "tenant_id": configured[sid].tenant_id, "config_digest": _digest(configured[sid])}
                    for sid in ids
                ],
            }
        )
    if not targets or (source_ids is not None and used_sources != set(source_ids)):
        raise HTTPException(422, "No matching knowledge resources for the selected identity sources")
    job_id = str(uuid.uuid4())
    value = {
        "job_id": job_id,
        "agent_id": agent_id,
        "state": "QUEUED",
        "phase": "QUEUED",
        "created_at": datetime.now(UTC).isoformat(),
        "created_by": admin_id,
        "knowledge_resource_ids": [t["knowledge_resource_id"] for t in targets],
        "identity_source_ids": sorted(used_sources),
        "targets": targets,
        "resources_total": len(targets),
        "skip_untracked_missing": True,
        "resources_skipped": 0,
        "resources_completed": 0,
        "completed": [],
        "owner": None,
        "lease_until": None,
        "error": None,
    }
    # Keep only the latest task per Agent; active tasks cannot be replaced.
    if prior is not None:
        await session.delete(prior)
    session.add(SystemConfig(config_key=PREFIX + job_id, config_value=json.dumps(value), config_version=1))
    if pointer:
        await write(session, pointer, {"job_id": job_id})
    else:
        session.add(SystemConfig(config_key=pointer_key, config_value=json.dumps({"job_id": job_id}), config_version=1))
    await session.flush()
    return public(value)


async def _claim(factory, job_id):
    async with factory() as session:
        row = await session.get(SystemConfig, PREFIX + job_id)
        if row is None:
            return None
        value = json.loads(row.config_value)
        if value["state"] not in {"QUEUED", "RUNNING"}:
            return None
        if (
            value["state"] == "RUNNING"
            and value.get("lease_until")
            and datetime.fromisoformat(value["lease_until"]) > datetime.now(UTC)
        ):
            return None
        owner = str(uuid.uuid4())
        await write(
            session,
            row,
            {
                **value,
                "state": "RUNNING",
                "owner": owner,
                "lease_until": (datetime.now(UTC) + timedelta(seconds=LEASE_SECONDS)).isoformat(),
                "started_at": value.get("started_at") or datetime.now(UTC).isoformat(),
            },
        )
        await session.commit()
        return owner


async def _owned(session, job_id, owner):
    row = await session.get(SystemConfig, PREFIX + job_id, populate_existing=True)
    if row is None:
        raise HTTPException(409, "Refresh ownership changed")
    value = json.loads(row.config_value)
    if (
        value.get("owner") != owner
        or value["state"] != "RUNNING"
        or datetime.fromisoformat(value["lease_until"]) <= datetime.now(UTC)
    ):
        raise HTTPException(409, "Refresh ownership changed")
    return row, value


async def _pull(factory, target, client_type):
    pulled = []
    total = 0
    for spec in target["sources"]:
        async with factory() as session:
            source = await session.get(EnterpriseIdentitySource, spec["id"])
            if source is None or source.tenant_id != spec["tenant_id"] or _digest(source) != spec["config_digest"]:
                raise RefreshPreconditionError(409, "Refresh configuration changed", "REFRESH_SOURCE_CHANGED")
            config = _configuration(source)
        client = (acl_snapshot_client_for(source.provider) if source.provider == IdentitySourceProvider.DINGTALK and client_type is FeishuAclMembershipSnapshotClient else client_type)(**config)
        snapshots = []
        # Complete every page before starting the PAS publication transaction.
        async with aclosing(
            client.iter_knowledge_scopes(
                tenant_key=spec["tenant_id"].strip(),
                space_id=target["space_id"],
                kb_id=target["kb_id"],
                max_grants_per_snapshot=MAX_GRANTS_PER_KB - total,
            )
        ) as stream:
            async for snapshot in stream:
                if snapshot.space_id != target["space_id"] or snapshot.kb_id != target["kb_id"]:
                    raise ExternalKnowledgeScopeSyncError("Unexpected filtered snapshot")
                total += len(snapshot.grants)
                if total > MAX_GRANTS_PER_KB:
                    raise HTTPException(413, "KB refresh grant limit exceeded")
                snapshots.append(snapshot)
        pulled.append((spec, snapshots))
    if not any(snapshots for _, snapshots in pulled):
        raise RefreshPreconditionError(
            409, "No complete permission snapshot; existing bindings retained", "REFRESH_SNAPSHOT_MISSING"
        )
    return pulled


async def _source_has_tracked_permissions(session, agent_id, source_id, resource_id):
    """A missing source cannot discard an imported snapshot or derived binding."""
    if await session.scalar(
        select(ExternalKnowledgeScopeSnapshot.id).where(
            ExternalKnowledgeScopeSnapshot.identity_source_id == source_id,
            ExternalKnowledgeScopeSnapshot.knowledge_resource_id == resource_id,
        ).limit(1)
    ):
        return True
    scopes = await session.scalars(
        select(AgentKnowledgeScope.knowledge_resource_ids_json).where(
            AgentKnowledgeScope.agent_id == agent_id,
            AgentKnowledgeScope.identity_source_id == source_id,
            AgentKnowledgeScope.origin == AgentKnowledgeScopeOrigin.EXTERNAL_SYNC,
        )
    )
    return any(resource_id in json.loads(raw) for raw in scopes)


async def _skip_untracked_missing(factory, job_id, owner, target):
    """Missing snapshots never mean revoked permissions. Skip only untouched KBs."""
    async with factory() as session:
        row = await session.get(SystemConfig, PREFIX + job_id)
        agent_id = json.loads(row.config_value)["agent_id"]
        await session.rollback()
        await lock_agent_knowledge_configuration(session, agent_id)
        row, value = await _owned(session, job_id, owner)
        if not value.get("skip_untracked_missing", False):
            return False
        _agent_enabled(await session.get(Agent, agent_id, populate_existing=True))
        resource = await session.get(KnowledgeResource, target["knowledge_resource_id"])
        scope = await resolve_agent_global_resource_scope(session, agent_id)
        if (
            resource is None
            or not resource.enabled
            or resource.sync_status != KnowledgeResourceSyncStatus.ACTIVE
            or not scope.allows(resource)
        ):
            raise RefreshPreconditionError(409, "Agent knowledge resource range changed", "REFRESH_RESOURCE_CHANGED")
        if (
            resource.space_id != target["space_id"] or resource.kb_id != target["kb_id"]
            or resource.polarrag_instance_id != target.get("polarrag_instance_id", resource.polarrag_instance_id)
        ):
            raise RefreshPreconditionError(409, "Agent knowledge resource range changed", "REFRESH_RESOURCE_CHANGED")
        source_ids = [spec["id"] for spec in target["sources"]]
        for spec in target["sources"]:
            source = await session.get(EnterpriseIdentitySource, spec["id"])
            if source is None or source.tenant_id != spec["tenant_id"] or _digest(source) != spec["config_digest"]:
                raise RefreshPreconditionError(409, "Refresh configuration changed", "REFRESH_SOURCE_CHANGED")
            _configuration(source)
            if not await session.scalar(
                select(EnterpriseIdentitySourceSpaceBinding.identity_source_id).where(
                    EnterpriseIdentitySourceSpaceBinding.identity_source_id == source.id,
                    EnterpriseIdentitySourceSpaceBinding.knowledge_space_id == resource.knowledge_space_id,
                )
            ):
                raise RefreshPreconditionError(409, "Identity source Space binding changed", "REFRESH_SOURCE_CHANGED")
        if await session.scalar(
            select(ExternalKnowledgeScopeSnapshot.id)
            .where(
                ExternalKnowledgeScopeSnapshot.identity_source_id.in_(source_ids),
                ExternalKnowledgeScopeSnapshot.knowledge_resource_id == resource.id,
            )
            .limit(1)
        ):
            return False
        raw_scopes = await session.scalars(
            select(AgentKnowledgeScope.knowledge_resource_ids_json).where(
                AgentKnowledgeScope.agent_id == agent_id,
                AgentKnowledgeScope.identity_source_id.in_(source_ids),
                AgentKnowledgeScope.origin == AgentKnowledgeScopeOrigin.EXTERNAL_SYNC,
            )
        )
        if any(resource.id in json.loads(raw) for raw in raw_scopes):
            return False
        completed = value["completed"] + [
            {
                "knowledge_resource_id": resource.id,
                "outcome": "SKIPPED",
                "reason": "NO_IMPORTED_OR_UPSTREAM_PERMISSION_SNAPSHOT",
            }
        ]
        done = len(completed) == value["resources_total"]
        await write(
            session,
            row,
            {
                **value,
                "completed": completed,
                "resources_completed": len(completed),
                "resources_skipped": value.get("resources_skipped", 0) + 1,
                "state": "SUCCEEDED" if done else "RUNNING",
                "phase": "COMPLETED" if done else "FETCHING",
                "finished_at": datetime.now(UTC).isoformat() if done else None,
                "lease_until": None if done else (datetime.now(UTC) + timedelta(seconds=LEASE_SECONDS)).isoformat(),
            },
        )
        await session.commit()
        return True


async def _publish(factory, job_id, owner, target, pulled, fetch_seconds=0):
    publish_started = time.monotonic()
    async with factory() as session:
        # The same lock order as mode switches and the existing importer.
        value_row = await session.get(SystemConfig, PREFIX + job_id)
        agent_id = json.loads(value_row.config_value)["agent_id"]
        await session.rollback()
        await lock_agent_knowledge_configuration(session, agent_id)
        row, value = await _owned(session, job_id, owner)
        _agent_enabled(await session.get(Agent, agent_id, populate_existing=True))
        resource = await session.get(KnowledgeResource, target["knowledge_resource_id"])
        base = await resolve_agent_global_resource_scope(session, agent_id)
        if (
            resource is None
            or not resource.enabled
            or resource.sync_status != KnowledgeResourceSyncStatus.ACTIVE
            or not base.allows(resource)
            or (resource.space_id, resource.kb_id) != (target["space_id"], target["kb_id"])
            or resource.polarrag_instance_id != target.get("polarrag_instance_id", resource.polarrag_instance_id)
        ):
            raise RefreshPreconditionError(409, "Agent knowledge resource range changed", "REFRESH_RESOURCE_CHANGED")
        # Fence job publication against another worker before touching snapshots.
        await write(session, row, {**value, "phase": "PUBLISHING"})
        counts = {"snapshots_processed": 0, "bindings_projected": 0}
        generations = []
        skipped_sources = []
        for spec, snapshots in pulled:
            source = await session.get(EnterpriseIdentitySource, spec["id"], populate_existing=True)
            if source is None or source.tenant_id != spec["tenant_id"] or _digest(source) != spec["config_digest"]:
                raise RefreshPreconditionError(409, "Refresh configuration changed", "REFRESH_SOURCE_CHANGED")
            _configuration(source)
            bound = await session.scalar(
                select(EnterpriseIdentitySourceSpaceBinding.id).where(
                    EnterpriseIdentitySourceSpaceBinding.identity_source_id == source.id,
                    EnterpriseIdentitySourceSpaceBinding.knowledge_space_id == resource.knowledge_space_id,
                )
            )
            if bound is None:
                raise RefreshPreconditionError(409, "Identity source Space binding changed", "REFRESH_SOURCE_CHANGED")
            if not snapshots:
                # Check under the publication lock, including state imported since the pull.
                if await _source_has_tracked_permissions(session, agent_id, source.id, resource.id):
                    raise RefreshPreconditionError(
                        409, "No complete permission snapshot; existing bindings retained", "REFRESH_SNAPSHOT_MISSING"
                    )
                skipped_sources.append(source.id)
                continue
            if source.provider.value == "dingtalk":
                source.permission_observed_at = datetime.now(UTC)
                source.permission_error = None
            current_ids = {s.snapshot_id for s in snapshots}
            for snapshot in snapshots:
                result = await import_external_knowledge_scope_snapshot(
                    session,
                    identity_source_id=source.id,
                    agent_ids={agent_id},
                    reuse_unchanged_grants=True,
                    snapshot=ImportedSnapshot(
                        snapshot_id=snapshot.snapshot_id,
                        tenant_key=snapshot.tenant_key,
                        source_scope_type=snapshot.source_scope_type,
                        source_scope_id=snapshot.source_scope_id,
                        source_scope_name=snapshot.source_scope_name,
                        space_id=snapshot.space_id,
                        kb_id=snapshot.kb_id,
                        generation=snapshot.generation,
                        fingerprint=snapshot.fingerprint,
                        status=snapshot.status,
                        source_updated_at=snapshot.source_updated_at,
                        grants=tuple(ImportedGrant(g.subject_type, g.subject_id) for g in snapshot.grants),
                    ),
                )
                counts["snapshots_processed"] += 1
                counts["bindings_projected"] += result["bindings_projected"]
                generations.append(
                    {
                        "identity_source_id": source.id,
                        "snapshot_id": snapshot.snapshot_id,
                        "generation": snapshot.generation,
                    }
                )
            # Remove obsolete shared snapshot catalog entries; other Agents' binding
            # rows remain untouched and will consume the new catalog on later refresh.
            stale_ids = list(
                await session.scalars(
                    select(ExternalKnowledgeScopeSnapshot.id).where(
                        ExternalKnowledgeScopeSnapshot.identity_source_id == source.id,
                        ExternalKnowledgeScopeSnapshot.knowledge_resource_id == resource.id,
                        ExternalKnowledgeScopeSnapshot.id.not_in(current_ids),
                    )
                )
            )
            scope_ids = list(
                await session.scalars(
                    select(AgentKnowledgeScope.id).where(
                        AgentKnowledgeScope.agent_id == agent_id,
                        AgentKnowledgeScope.identity_source_id == source.id,
                        AgentKnowledgeScope.origin == AgentKnowledgeScopeOrigin.EXTERNAL_SYNC,
                        AgentKnowledgeScope.external_scope_id.not_in(current_ids),
                        AgentKnowledgeScope.knowledge_resource_ids_json
                        == json.dumps([resource.id], separators=(",", ":")),
                    )
                )
            )
            if scope_ids:
                await session.execute(
                    delete(AgentKnowledgeScopeBinding).where(AgentKnowledgeScopeBinding.scope_id.in_(scope_ids))
                )
                await session.execute(delete(AgentKnowledgeScope).where(AgentKnowledgeScope.id.in_(scope_ids)))
            if stale_ids:
                await session.execute(
                    delete(ExternalKnowledgeScopeGrant).where(ExternalKnowledgeScopeGrant.snapshot_id.in_(stale_ids))
                )
                await session.execute(
                    delete(ExternalKnowledgeScopeSnapshot).where(ExternalKnowledgeScopeSnapshot.id.in_(stale_ids))
                )
        completed = value["completed"] + [
            {
                "knowledge_resource_id": resource.id,
                **counts,
                "generations": generations,
                "identity_sources_skipped": skipped_sources,
                "grants_fetched": sum(len(s.grants) for _, snapshots in pulled for s in snapshots),
                "fetch_seconds": round(fetch_seconds, 4),
                "publish_seconds_before_commit": round(time.monotonic() - publish_started, 4),
            }
        ]
        done = len(completed) == value["resources_total"]
        await write(
            session,
            row,
            {
                **value,
                "completed": completed,
                "resources_completed": len(completed),
                "state": "SUCCEEDED" if done else "RUNNING",
                "phase": "COMPLETED" if done else "FETCHING",
                "finished_at": datetime.now(UTC).isoformat() if done else None,
                "lease_until": None if done else (datetime.now(UTC) + timedelta(seconds=LEASE_SECONDS)).isoformat(),
            },
        )
        await session.commit()


async def run(job_id, *, session_factory=None, client_factory=None):
    async with _worker_semaphore():
        await _run(job_id, session_factory=session_factory, client_factory=client_factory)


def start(job_id):
    """Keep execution independent of the request and its cancellation."""
    task = asyncio.create_task(run(job_id))
    _started_tasks.add(task)
    task.add_done_callback(_finished_task)


def _finished_task(task):
    _started_tasks.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.warning(
            "knowledge_permission_refresh.worker_failed", extra={"error_type": type(task.exception()).__name__}
        )


async def wait_for_result(agent_id, job_id, timeout_seconds, *, session_factory=None):
    factory = session_factory or get_session_factory()
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    delay = 0.1
    while True:
        # A fresh, short read transaction observes other workers' committed changes.
        async with factory() as session:
            result = await status(session, agent_id, job_id)
        remaining = deadline - asyncio.get_running_loop().time()
        if result["state"] in {"SUCCEEDED", "PARTIAL", "FAILED"} or remaining <= 0:
            return result
        await asyncio.sleep(min(delay, remaining))
        delay = min(delay * 2, 1.0)


async def _run(job_id, *, session_factory=None, client_factory=None):
    factory = session_factory or get_session_factory()
    try:
        owner = await _claim(factory, job_id)
    except HTTPException as exc:
        if exc.status_code == 409:
            return
        raise
    if owner is None:
        return
    try:
        while True:
            async with factory() as session:
                row, value = await _owned(session, job_id, owner)
                target = value["targets"][value["resources_completed"]]
                await write(
                    session,
                    row,
                    {**value, "phase": "FETCHING", "current_knowledge_resource_id": target["knowledge_resource_id"]},
                )
                await session.commit()
            async with asyncio.timeout(KB_TIMEOUT_SECONDS):
                fetch_started = time.monotonic()
                try:
                    pulled = await _pull(factory, target, client_factory or FeishuAclMembershipSnapshotClient)
                except HTTPException as exc:
                    if (
                        getattr(exc, "code", None) != "REFRESH_SNAPSHOT_MISSING"
                        or not await _skip_untracked_missing(factory, job_id, owner, target)
                    ):
                        raise
                else:
                    await _publish(factory, job_id, owner, target, pulled, time.monotonic() - fetch_started)
            async with factory() as session:
                row = await session.get(SystemConfig, PREFIX + job_id)
                if row is None:  # A newer task may have reclaimed this terminal checkpoint.
                    return
                value = json.loads(row.config_value)
                if value["state"] == "SUCCEEDED":
                    return
    except asyncio.CancelledError:
        # Keep durable RUNNING checkpoint; another worker resumes after lease expiry.
        raise
    except Exception as exc:
        code = "REFRESH_FAILED"
        if isinstance(exc, TimeoutError):
            code = "REFRESH_TIMEOUT"
        elif isinstance(exc, AclMembershipSnapshotCapacityError) or (
            isinstance(exc, HTTPException) and exc.status_code == 413
        ):
            code = "REFRESH_CAPACITY_LIMIT"
        elif isinstance(exc, ExternalKnowledgeScopeSyncError):
            code = "REFRESH_SNAPSHOT_REJECTED"
        elif isinstance(exc, HTTPException):
            code = getattr(exc, "code", "REFRESH_PRECONDITION_FAILED")
        logger.warning("knowledge_permission_refresh.failed", extra={"job_id": job_id, "error": code})
        async with factory() as session:
            try:
                row, value = await _owned(session, job_id, owner)
                await write(
                    session,
                    row,
                    {
                        **value,
                        "state": "PARTIAL"
                        if any(item.get("outcome") != "SKIPPED" for item in value["completed"])
                        else "FAILED",
                        "phase": "FAILED",
                        "resources_unprocessed": value["resources_total"] - value["resources_completed"],
                        "error": code,
                        "finished_at": datetime.now(UTC).isoformat(),
                        "lease_until": None,
                    },
                )
                await session.commit()
            except HTTPException as fence:
                if fence.status_code != 409:
                    raise


async def resume_once(factory):
    async with factory() as session:
        pointers = list(
            await session.scalars(
                select(SystemConfig).where(SystemConfig.config_key.startswith("knowledge_permission_refresh.agent."))
            )
        )
        rows = []
        for pointer in pointers:
            row = await session.get(SystemConfig, PREFIX + json.loads(pointer.config_value)["job_id"])
            if row is not None:
                rows.append(row)
        ids = []
        now = datetime.now(UTC)
        for row in rows:
            value = json.loads(row.config_value)
            if value["state"] == "QUEUED" or (
                value["state"] == "RUNNING" and datetime.fromisoformat(value["lease_until"]) <= now
            ):
                ids.append(value["job_id"])
    await asyncio.gather(*(run(job_id, session_factory=factory) for job_id in ids))


async def refresh_loop(factory):
    try:
        await _refresh_loop(factory)
    finally:
        tasks = list(_started_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _refresh_loop(factory):
    while True:
        try:
            await resume_once(factory)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Driver/configuration messages may contain credentials; log type only.
            logger.warning("knowledge_permission_refresh.poll_failed", extra={"error_type": type(exc).__name__})
        await asyncio.sleep(10)
