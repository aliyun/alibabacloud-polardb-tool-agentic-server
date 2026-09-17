from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from server.management.settings import (
    ListenerSettings,
    ManagedIdentitySettings,
)
from server.runtime import RuntimePhase


def _valid_request() -> dict[str, object]:
    return {
        "protocol_version": 1,
        "target": {"instance_id": "pmcp-test", "generation": 1},
        "actor": {
            "type": "managed_initializer",
            "subject_id": None,
        },
        "command": {
            "module": "core_admin",
            "action": "save_draft",
            "parameter": "username",
            "value": "admin",
            "expected_revision": 0,
            "idempotency_key": "stable-key",
        },
    }


def test_managed_envelope_accepts_one_parameter() -> None:
    from server.management.types import ManagedEnvelope

    envelope = ManagedEnvelope.model_validate(_valid_request())

    assert envelope.target.instance_id == "pmcp-test"
    assert envelope.command.parameter == "username"
    assert envelope.actor.type == "managed_initializer"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda body: body.pop("target"),
        lambda body: body["actor"].update(type="admin"),
        lambda body: body["command"].update(action="delete"),
        lambda body: body["command"].update(module="user_sso"),
        lambda body: body["command"].update(parameter="client_secret"),
        lambda body: body.update(extra_field=True),
        lambda body: body["command"].update(actor="admin"),
    ],
)
def test_managed_envelope_rejects_unknown_or_missing_fields(
    mutation,
) -> None:
    from server.management.types import ManagedEnvelope

    body = _valid_request()
    mutation(body)

    with pytest.raises(ValidationError):
        ManagedEnvelope.model_validate(body)


def test_managed_envelope_rejects_multiple_parameter_object() -> None:
    from server.management.types import ManagedEnvelope

    body = _valid_request()
    body["command"].update(
        value={"username": "admin", "password": "must-not-leak"}
    )

    with pytest.raises(
        ValidationError,
        match="MULTIPLE_PARAMETERS_NOT_SUPPORTED",
    ):
        ManagedEnvelope.model_validate(body)


def test_action_parameter_combinations_fail_closed() -> None:
    from server.management.types import ManagedEnvelope

    body = _valid_request()
    body["command"].update(action="activate", parameter="username")

    with pytest.raises(ValidationError, match="CONFIG_OPERATION_NOT_ALLOWED"):
        ManagedEnvelope.model_validate(body)


def test_invalid_http_envelope_is_sanitized_and_makes_no_write() -> None:
    from server.management.app import create_management_app

    runtime = SimpleNamespace(
        phase=RuntimePhase.SETUP,
        error_code=None,
        application_state=SimpleNamespace(config_service=object()),
    )
    settings = ListenerSettings(
        business_port=18080,
        management_port=18081,
        auth_mode="trusted-network",
        token=None,
        managed_identity=ManagedIdentitySettings("pmcp-test", 1),
    )
    client = TestClient(create_management_app(runtime, settings))
    body = _valid_request()
    body["command"].update(
        value={"username": "admin", "password": "must-not-leak"}
    )

    response = client.post("/api/internal/v1/config", json=body)

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == (
        "MULTIPLE_PARAMETERS_NOT_SUPPORTED"
    )
    assert "must-not-leak" not in response.text
