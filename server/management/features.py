"""The managed writer for knowledge configuration and rollout admission.

Kept separate from the legacy one-parameter administrator initialization API.
"""

from __future__ import annotations

from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from server.configuration.types import ConfigActor, ConfigCommand, ConfigError
from server.management.identity import ManagedIdentityBinder, ManagedIdentityError
from server.management.types import ManagedTarget


class KnowledgeEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol_version: Literal[1] = 1
    target: ManagedTarget
    action: Literal[
        "status",
        "options",
        "config",
        "save_connection",
        "check_connection",
        "prepare_space",
        "test",
        "drain",
        "cancel_drain",
        "confirm_activation",
    ]
    parameters: dict = Field(default_factory=dict)


async def knowledge_command(runtime, settings, envelope: KnowledgeEnvelope):
    state = runtime.application_state
    service = getattr(state, "config_service", None)
    feature = getattr(state, "knowledge_runtime", None)
    identity = settings.managed_identity
    if service is None or feature is None or identity is None or not feature.managed:
        raise HTTPException(503, detail={"code": "MANAGEMENT_RUNTIME_UNAVAILABLE"})
    binding = await ManagedIdentityBinder(service.repository, identity).verify(envelope.target)
    if binding.status != "BOUND":
        raise ManagedIdentityError("INSTANCE_IDENTITY_UNBOUND")
    parameters = envelope.parameters
    action = envelope.action
    if action == "config":
        command = ConfigCommand.model_validate(parameters)
        if command.module != "knowledge" or command.action not in {"describe", "save_draft", "validate", "activate"}:
            raise ConfigError("CONFIG_OPERATION_NOT_ALLOWED", "Only knowledge configuration operations are accepted")
        return await service.execute(
            command,
            ConfigActor(
                scope=f"managed-knowledge:{identity.instance_id}:{identity.generation}",
                actor_type="system",
                instance_id=identity.instance_id,
                instance_generation=identity.generation,
            ),
        )
    from server.api import features as api

    if action == "status":
        return {
            "protocol_version": 1,
            "instance_id": identity.instance_id,
            "instance_generation": identity.generation,
            **await feature.status(),
        }
    if action == "options":
        return await api.options(_admin=None, service=service)
    if action in {"drain", "cancel_drain", "confirm_activation"}:
        body = (
            api.ActivationRequest.model_validate(parameters)
            if action == "confirm_activation"
            else api.RevisionRequest.model_validate(parameters)
        )
        desired = await feature.state.desired()
        if desired.revision != body.expected_revision:
            raise ConfigError("REVISION_CONFLICT", "Knowledge configuration changed")
        if action == "drain":
            await feature.state.begin_drain()
        elif action == "cancel_drain":
            await feature.state.cancel_drain(body.expected_revision)
        else:
            await feature.state.confirm_activation(body.expected_revision, body.replica_ids)
        return await feature.status()
    if action == "check_connection":
        return await api.check_connection(instance_id=_connection_id(parameters), feature=feature)
    if action == "test":
        return await api.read_test(body=api.ReadTestRequest.model_validate(parameters), feature=feature)
    # Audit onboarding under the existing managed administrator; never invent a user.
    from server.models import AuthProvider, User, UserRole, UserStatus

    document = await service.repository.get_module("core_admin")
    username = document.effective.config["username"] if document and document.effective else None
    async with service.repository.session_factory() as session:
        admin = await session.scalar(
            select(User).where(
                User.external_id == username,
                User.auth_provider == AuthProvider.BUILTIN,
                User.role == UserRole.ADMIN,
                User.status == UserStatus.ACTIVE,
            )
        )
    if admin is None:
        raise ConfigError("MANAGEMENT_ADMIN_UNAVAILABLE", "Initialize the managed administrator first")
    if action == "save_connection":
        return await api.save_connection(
            body=api.InstanceCreate.model_validate(parameters), admin=admin, feature=feature
        )
    return await api.prepare_space(
        instance_id=_connection_id(parameters),
        body=api.SpaceRequest.model_validate(parameters),
        admin=admin,
        feature=feature,
    )


def _connection_id(parameters):
    value = parameters.get("instance_id")
    if not isinstance(value, str) or not value or len(value) > 36:
        raise ConfigError("CONFIG_OPERATION_NOT_ALLOWED", "A saved connection is required")
    return value
