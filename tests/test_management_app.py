from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from server.management.settings import (
    ListenerSettings,
    ManagedIdentitySettings,
)
from server.runtime import RuntimePhase


def _runtime(
    *,
    phase: RuntimePhase = RuntimePhase.SETUP,
    application_state: object | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        phase=phase,
        error_code=None,
        application_state=application_state,
    )


def _trusted_settings() -> ListenerSettings:
    return ListenerSettings(
        business_port=18080,
        management_port=18081,
        auth_mode="trusted-network",
        token=None,
        managed_identity=ManagedIdentitySettings(
            instance_id="pmcp-test",
            generation=3,
        ),
    )


def _bearer_settings() -> ListenerSettings:
    return ListenerSettings(
        business_port=18080,
        management_port=18081,
        auth_mode="bearer-token",
        token=b"test-management-token",
        managed_identity=ManagedIdentitySettings(
            instance_id="pmcp-test",
            generation=3,
        ),
    )


def test_status_reports_explicit_sanitized_protocol_fields() -> None:
    from server.management.app import create_management_app

    repository = SimpleNamespace(global_version=lambda: None)

    async def global_version() -> int:
        return 7

    repository.global_version = global_version
    store = SimpleNamespace(
        repository=repository,
        config_version=7,
        last_error_code=None,
        local_errors={},
    )
    state = SimpleNamespace(
        runtime_access_policy=SimpleNamespace(mode="SETUP"),
        runtime_config_store=store,
        config_service=object(),
        secret_value="must-not-leak",
    )
    app = create_management_app(
        _runtime(application_state=state),
        _trusted_settings(),
    )

    response = TestClient(app).get("/api/internal/v1/status")

    assert response.status_code == 200
    assert response.json() == {
        "protocol_version": 1,
        "runtime_phase": "SETUP",
        "pas_state": "SETUP",
        "schema_status": "CURRENT",
        "config_status": "CURRENT",
        "desired_config_version": 7,
        "loaded_config_version": 7,
        "instance_id": "pmcp-test",
        "instance_generation": 3,
        "credential_state": None,
    }
    assert "must-not-leak" not in response.text
    assert "test-management-token" not in response.text


def test_schema_blocker_status_is_available_without_runtime_services() -> None:
    from server.management.app import create_management_app

    runtime = _runtime(phase=RuntimePhase.SCHEMA_OUTDATED)
    runtime.error_code = "DATABASE_SCHEMA_OUTDATED"
    app = create_management_app(runtime, _trusted_settings())

    response = TestClient(app).get("/api/internal/v1/status")

    assert response.status_code == 200
    assert response.json()["schema_status"] == "OUTDATED"
    assert response.json()["pas_state"] == "SCHEMA_OUTDATED"
    assert response.json()["config_status"] == "UNAVAILABLE"
    assert response.json()["desired_config_version"] is None


def test_trusted_network_accepts_status_without_token() -> None:
    from server.management.app import create_management_app

    response = TestClient(
        create_management_app(_runtime(), _trusted_settings())
    ).get("/api/internal/v1/status")

    assert response.status_code == 200


def test_bearer_auth_has_same_failure_for_missing_and_invalid_token(
    monkeypatch,
) -> None:
    from server.management import auth
    from server.management.app import create_management_app

    compared: list[tuple[bytes, bytes]] = []
    real_compare = auth.hmac.compare_digest

    def compare_digest(left: bytes, right: bytes) -> bool:
        compared.append((left, right))
        return real_compare(left, right)

    monkeypatch.setattr(auth.hmac, "compare_digest", compare_digest)
    client = TestClient(
        create_management_app(_runtime(), _bearer_settings())
    )

    missing = client.get("/api/internal/v1/status")
    invalid = client.get(
        "/api/internal/v1/status",
        headers={"Authorization": "Bearer wrong-token"},
    )
    valid = client.get(
        "/api/internal/v1/status",
        headers={
            "Authorization": "Bearer test-management-token"
        },
    )

    assert missing.status_code == invalid.status_code == 401
    assert missing.json() == invalid.json() == {
        "detail": {
            "code": "MANAGEMENT_UNAUTHORIZED",
            "message": "Management authentication failed",
        }
    }
    assert valid.status_code == 200
    assert len(compared) == 3


def test_config_commands_wait_for_shared_config_service() -> None:
    from server.management.app import create_management_app

    client = TestClient(
        create_management_app(_runtime(), _trusted_settings())
    )

    response = client.post("/api/internal/v1/config", json={})

    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "code": "MANAGEMENT_RUNTIME_UNAVAILABLE",
            "message": "PAS configuration runtime is unavailable",
        }
    }
