from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from server.auth.jwt_manager import _generate_rsa_key_pair
from server.configuration.registry import (
    MODULE_REGISTRY,
    OBSERVABILITY_SCHEMA_VERSION,
    SQL_SECURITY_SCHEMA_VERSION,
    ObservabilityConfig,
    SQLSecurityModuleConfig,
)
from server.configuration.repository import ConfigConflict, ConfigRepository
from server.configuration.types import (
    EffectiveConfig,
    ModuleDocument,
    ModuleState,
    SystemState,
)
from server.core.config_crypto import ConfigCrypto

AUTO_RESTORE_MODULES = frozenset(
    {"enterprise_identity_sync", "polarrag_tool_limits", "knowledge"}
)


@dataclass(frozen=True, slots=True)
class InitializationResult:
    system_state: SystemState
    bootstrap_token: str | None


def _active_document(
    module: str,
    config: dict,
    schema_version: int,
) -> ModuleDocument:
    return ModuleDocument(
        schema_version=schema_version,
        revision=1,
        workflow_state=ModuleState.ACTIVE,
        initial_state=MODULE_REGISTRY[module].initial_state,
        desired_state=ModuleState.ACTIVE,
        effective=EffectiveConfig(
            revision=1,
            state=ModuleState.ACTIVE,
            config=config,
        ),
    )


def _default_document(
    name: str,
    default_config: dict | None = None,
) -> ModuleDocument:
    definition = MODULE_REGISTRY[name]
    if default_config is None:
        default_config = (
            definition.model().model_dump(mode="json")
            if definition.initial_state in {ModuleState.ACTIVE, ModuleState.DRAFT}
            else {}
        )
    if definition.initial_state == ModuleState.ACTIVE:
        return _active_document(name, default_config, definition.schema_version)
    draft = default_config if definition.initial_state == ModuleState.DRAFT else None
    return ModuleDocument(
        schema_version=definition.schema_version,
        revision=0,
        workflow_state=definition.initial_state,
        initial_state=definition.initial_state,
        draft=draft,
    )


def _initial_documents(
    crypto: ConfigCrypto,
) -> dict[str, ModuleDocument]:
    private_key, public_key = _generate_rsa_key_pair()
    kid = secrets.token_hex(8)
    private_envelope = crypto.encrypt_field(
        private_key,
        module="token_security",
        field_path="private_key",
        schema_version=MODULE_REGISTRY["token_security"].schema_version,
    )
    documents: dict[str, ModuleDocument] = {}
    for name, definition in MODULE_REGISTRY.items():
        default_config = (
            definition.model().model_dump(mode="json")
            if definition.initial_state
            in {ModuleState.ACTIVE, ModuleState.DRAFT}
            else {}
        )
        if name == "token_security":
            default_config.update(
                {
                    "active_kid": kid,
                    "private_key": {
                        "$secret": private_envelope.model_dump(
                            mode="json"
                        )
                    },
                    "public_keys": {kid: public_key},
                }
            )
        documents[name] = _default_document(name, default_config)
    return documents


def _is_active_agent_token_auth(
    document: ModuleDocument,
) -> bool:
    return (
        document.workflow_state == ModuleState.ACTIVE
        and document.initial_state == ModuleState.ACTIVE
        and document.desired_state == ModuleState.ACTIVE
        and document.draft is None
        and document.effective is not None
        and document.effective.state == ModuleState.ACTIVE
        and document.effective.config == {"enabled": True}
    )


async def _converge_agent_token_auth(
    repository: ConfigRepository,
) -> None:
    document = await repository.get_module("agent_token_auth")
    if document is not None and _is_active_agent_token_auth(document):
        return

    definition = MODULE_REGISTRY["agent_token_auth"]
    next_revision = (document.revision if document is not None else 0) + 1
    active = ModuleDocument(
        schema_version=definition.schema_version,
        workflow_state=ModuleState.ACTIVE,
        initial_state=ModuleState.ACTIVE,
        desired_state=ModuleState.ACTIVE,
        effective=EffectiveConfig(
            revision=next_revision,
            state=ModuleState.ACTIVE,
            config=definition.model().model_dump(mode="json"),
        ),
    )
    try:
        await repository.compare_and_set_module(
            "agent_token_auth",
            expected_revision=document.revision if document is not None else 0,
            document=active,
        )
    except ConfigConflict:
        # Another replica may have converged the same historical default.
        return


async def _restore_missing_modules(
    repository: ConfigRepository,
) -> None:
    existing = await repository.list_modules()
    missing = [name for name in MODULE_REGISTRY if name not in existing]
    if not missing:
        return
    unsupported = [name for name in missing if name not in AUTO_RESTORE_MODULES]
    if unsupported:
        raise RuntimeError(f"{unsupported[0]} cannot be restored automatically")
    for name in missing:
        await repository.ensure_module(
            name, _default_document(name, {"enabled": True} if name == "knowledge" else None)
        )


def _audit_values(config: dict) -> tuple[bool, int]:
    enabled = config.get("audit_enabled")
    retention_days = config.get("audit_retention_days")
    if not isinstance(enabled, bool) or (
        not isinstance(retention_days, int)
        or isinstance(retention_days, bool)
        or retention_days < 1
    ):
        raise RuntimeError("legacy SQL audit configuration is invalid")
    return enabled, retention_days


def _migrate_config_pair(
    sql_config: dict,
    observability_config: dict,
) -> tuple[dict, dict]:
    enabled, retention_days = _audit_values(sql_config)
    migrated_sql = dict(sql_config)
    migrated_sql.pop("audit_enabled", None)
    migrated_sql.pop("audit_retention_days", None)
    migrated_observability = {
        **observability_config,
        "audit_enabled": observability_config.get(
            "audit_enabled",
            enabled,
        ),
        "audit_retention_days": observability_config.get(
            "audit_retention_days",
            retention_days,
        ),
    }
    try:
        normalized_sql = SQLSecurityModuleConfig.model_validate(
            migrated_sql
        ).model_dump(mode="json")
        normalized_observability = ObservabilityConfig.model_validate(
            migrated_observability
        ).model_dump(mode="json")
    except Exception as exc:
        raise RuntimeError("global audit configuration migration failed") from exc
    return normalized_sql, normalized_observability


def _effective_with_config(
    document: ModuleDocument,
    config: dict,
    *,
    increment_revision: bool,
):
    if document.effective is None:
        raise RuntimeError("active audit configuration is missing")
    return document.effective.model_copy(
        update={
            "revision": document.revision + int(increment_revision),
            "config": config,
        }
    )


def migrate_global_audit_documents(
    sql_document: ModuleDocument,
    observability_document: ModuleDocument,
    *,
    increment_revision: bool = True,
) -> tuple[ModuleDocument, ModuleDocument]:
    if (
        sql_document.schema_version != 1
        or observability_document.schema_version != 1
    ):
        raise RuntimeError("unsupported global audit configuration schema")
    if (
        sql_document.workflow_state == ModuleState.VALIDATING
        or observability_document.workflow_state == ModuleState.VALIDATING
    ):
        raise RuntimeError(
            "global audit configuration cannot migrate during validation"
        )
    if sql_document.effective is None or observability_document.effective is None:
        raise RuntimeError("active audit configuration is missing")

    sql_effective, observability_effective = _migrate_config_pair(
        sql_document.effective.config,
        observability_document.effective.config,
    )
    sql_draft = sql_document.draft
    observability_draft = observability_document.draft
    migrated_sql_draft: dict | None = None
    migrated_observability_draft: dict | None = None
    if sql_draft is not None:
        draft_base = (
            observability_draft
            if observability_draft is not None
            else observability_document.effective.config
        )
        migrated_sql_draft, migrated_observability_draft = (
            _migrate_config_pair(sql_draft, draft_base)
        )
    elif observability_draft is not None:
        _, migrated_observability_draft = _migrate_config_pair(
            sql_document.effective.config,
            observability_draft,
        )

    sql_updates: dict = {
        "schema_version": SQL_SECURITY_SCHEMA_VERSION,
        "revision": sql_document.revision + int(increment_revision),
        "effective": _effective_with_config(
            sql_document,
            sql_effective,
            increment_revision=increment_revision,
        ),
        "draft": migrated_sql_draft,
        "last_validation": None,
        "validation_operation": None,
        "last_error_code": None,
    }
    if (
        migrated_sql_draft is None
        or migrated_sql_draft == sql_effective
    ):
        sql_updates.update(
            {
                "workflow_state": sql_document.effective.state,
                "draft": None,
            }
        )
    else:
        sql_updates["workflow_state"] = ModuleState.DRAFT

    observability_updates: dict = {
        "schema_version": OBSERVABILITY_SCHEMA_VERSION,
        "revision": observability_document.revision
        + int(increment_revision),
        "effective": _effective_with_config(
            observability_document,
            observability_effective,
            increment_revision=increment_revision,
        ),
        "draft": migrated_observability_draft,
        "last_validation": None,
        "validation_operation": None,
        "last_error_code": None,
    }
    if migrated_observability_draft is not None:
        observability_updates["workflow_state"] = ModuleState.DRAFT

    return (
        sql_document.model_copy(update=sql_updates),
        observability_document.model_copy(
            update=observability_updates
        ),
    )


def project_global_audit_documents(
    documents: dict[str, ModuleDocument],
) -> dict[str, ModuleDocument]:
    sql_document = documents.get("sql_security")
    observability_document = documents.get("observability")
    if sql_document is None and observability_document is None:
        return documents
    if sql_document is None or observability_document is None:
        raise RuntimeError(
            "audit configuration modules must exist together"
        )
    if (
        sql_document.schema_version,
        observability_document.schema_version,
    ) == (
        SQL_SECURITY_SCHEMA_VERSION,
        OBSERVABILITY_SCHEMA_VERSION,
    ):
        return documents
    projected_sql, projected_observability = (
        migrate_global_audit_documents(
            sql_document,
            observability_document,
            increment_revision=False,
        )
    )
    return {
        **documents,
        "sql_security": projected_sql,
        "observability": projected_observability,
    }


async def initialize_configuration(
    repository: ConfigRepository,
    crypto: ConfigCrypto,
    *,
    managed: bool = False,
) -> InitializationResult:
    existing = await repository.get_config_row("setup.status")
    if existing is not None:
        await _converge_agent_token_auth(repository)
        await _restore_missing_modules(repository)
        payload = json.loads(existing.config_value)
        return InitializationResult(
            system_state=SystemState(payload["system_state"]),
            bootstrap_token=None,
        )

    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc)
    setup_value = json.dumps(
        {
            "schema_version": 1,
            "system_state": SystemState.SETUP.value,
            "initialized_at": now.isoformat(),
            "ready_at": None,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    created = await repository.initialize_rows(
        _initial_documents(crypto),
        setup_value=setup_value,
        token_hash=token_hash,
        token_expires_at=now + timedelta(minutes=15),
    )
    return InitializationResult(
        system_state=SystemState.SETUP,
        bootstrap_token=token if created and not managed else None,
    )


def _as_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


async def verify_bootstrap_token(
    repository: ConfigRepository,
    token: str,
) -> bool:
    claim = await repository.get_bootstrap_claim()
    now = datetime.now(timezone.utc)
    if (
        claim is None
        or claim.consumed_at is not None
        or _as_aware(claim.expires_at) <= now
        or claim.failed_attempts >= 10
    ):
        return False
    candidate = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if hmac.compare_digest(candidate, claim.token_hash):
        return True
    await repository.record_bootstrap_failure()
    return False


async def rotate_bootstrap_token(
    repository: ConfigRepository,
) -> str:
    """Invalidate any prior claim and return a new short-lived token once."""
    token = secrets.token_urlsafe(32)
    await repository.replace_bootstrap_claim(
        token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
    )
    return token
