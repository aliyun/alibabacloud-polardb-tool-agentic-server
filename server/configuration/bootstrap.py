from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from server.auth.jwt_manager import _generate_rsa_key_pair
from server.configuration.registry import MODULE_REGISTRY
from server.configuration.repository import ConfigConflict, ConfigRepository
from server.configuration.types import (
    EffectiveConfig,
    ModuleDocument,
    ModuleState,
    SystemState,
)
from server.core.config_crypto import ConfigCrypto


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
        if definition.initial_state == ModuleState.ACTIVE:
            documents[name] = _active_document(
                name, default_config, definition.schema_version
            )
            continue
        draft = (
            default_config
            if definition.initial_state == ModuleState.DRAFT
            else None
        )
        documents[name] = ModuleDocument(
            schema_version=definition.schema_version,
            revision=0,
            workflow_state=definition.initial_state,
            initial_state=definition.initial_state,
            draft=draft,
        )
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


async def initialize_configuration(
    repository: ConfigRepository,
    crypto: ConfigCrypto,
) -> InitializationResult:
    existing = await repository.get_config_row("setup.status")
    if existing is not None:
        await _converge_agent_token_auth(repository)
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
        bootstrap_token=token if created else None,
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
