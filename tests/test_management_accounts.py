from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.configuration.repository import ConfigRepository
from server.configuration.service import ConfigService
from server.core.config_crypto import ConfigCrypto
from server.auth.builtin import authenticate_builtin, hash_password
from server.management.app import create_management_app
from server.management.settings import (
    ListenerSettings,
    ManagedIdentitySettings,
)
from server.runtime import RuntimePhase
from server.models import (
    AuthProvider,
    Base,
    ManagedInstanceBinding,
    PasswordState,
    User,
    UserRole,
    UserStatus,
)


OLD_PASSWORD = "old-managed-password"
NEW_PASSWORD = "new-managed-password"
ROOT_KEY = b"01234567890123456789012345678901"


def _settings() -> ListenerSettings:
    return ListenerSettings(
        business_port=18080,
        management_port=18081,
        auth_mode="trusted-network",
        token=None,
        managed_identity=ManagedIdentitySettings("pmcp-a", 1),
    )


def _password_body(
    *,
    operation: str = "RESET",
    old_password: str | None = None,
    new_password: str = NEW_PASSWORD,
) -> dict[str, object]:
    return {
        "protocol_version": 1,
        "target": {"instance_id": "pmcp-a", "generation": 1},
        "actor": {
            "type": "control_user",
            "subject_id": "opaque-subject",
        },
        "operation": operation,
        "old_password": old_password,
        "new_password": new_password,
        "idempotency_key": "openapi-request-id",
    }


@pytest.fixture
async def described_admin_context(tmp_path):
    database = tmp_path / "managed-account-describe.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = ConfigRepository(factory)
    await repository.ensure_setup_status()
    async with factory() as session:
        session.add(
            User(
                external_id="admin",
                display_name="Administrator",
                auth_provider=AuthProvider.BUILTIN,
                password_hash="unusable-placeholder",
                password_state=PasswordState.RESET_REQUIRED,
                role=UserRole.ADMIN,
                status=UserStatus.ACTIVE,
            )
        )
        await session.commit()
    service = ConfigService(repository, ConfigCrypto(ROOT_KEY))
    yield factory, repository, service
    await engine.dispose()


@pytest.fixture
async def reset_required_account_context(tmp_path):
    database = tmp_path / "managed-account-mutation.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = ConfigRepository(factory)
    await repository.ensure_setup_status()
    await repository.bind_managed_identity(
        instance_id="pmcp-a",
        generation=1,
    )
    async with factory() as session:
        session.add(
            User(
                external_id="admin",
                display_name="Administrator",
                auth_provider=AuthProvider.BUILTIN,
                password_hash=hash_password(OLD_PASSWORD),
                password_state=PasswordState.RESET_REQUIRED,
                role=UserRole.ADMIN,
                status=UserStatus.ACTIVE,
            )
        )
        await session.commit()
    service = ConfigService(repository, ConfigCrypto(ROOT_KEY))
    yield factory, repository, service
    await engine.dispose()


def _account_service(config_service: ConfigService):
    from server.management.accounts import ManagedAccountService
    from server.management.identity import ManagedIdentityBinder

    return ManagedAccountService(
        config_service,
        ManagedIdentityBinder(
            config_service.repository,
            ManagedIdentitySettings("pmcp-a", 1),
        ),
    )


def _password_envelope(
    *,
    operation: str = "RESET",
    old_password: str | None = None,
    new_password: str = NEW_PASSWORD,
    idempotency_key: str = "openapi-request-id",
    generation: int = 1,
):
    from server.management.account_types import ManagedAccountPasswordEnvelope

    body = _password_body(
        operation=operation,
        old_password=old_password,
        new_password=new_password,
    )
    body["idempotency_key"] = idempotency_key
    body["target"] = {"instance_id": "pmcp-a", "generation": generation}
    return ManagedAccountPasswordEnvelope.model_validate(body)


def test_reset_request_rejects_old_password() -> None:
    from server.management.account_types import ManagedAccountPasswordEnvelope

    with pytest.raises(ValidationError):
        ManagedAccountPasswordEnvelope.model_validate(_password_body(old_password=OLD_PASSWORD))


def test_modify_request_requires_old_password() -> None:
    from server.management.account_types import ManagedAccountPasswordEnvelope

    with pytest.raises(ValidationError):
        ManagedAccountPasswordEnvelope.model_validate(_password_body(operation="MODIFY"))


def test_password_request_requires_protocol_version() -> None:
    from server.management.account_types import ManagedAccountPasswordEnvelope

    body = _password_body()
    del body["protocol_version"]

    with pytest.raises(ValidationError):
        ManagedAccountPasswordEnvelope.model_validate(body)


def test_password_request_repr_hides_password_fields() -> None:
    from server.management.account_types import ManagedAccountPasswordEnvelope

    model = ManagedAccountPasswordEnvelope.model_validate(_password_body(operation="MODIFY", old_password=OLD_PASSWORD))

    rendered = repr(model)
    assert OLD_PASSWORD not in rendered
    assert NEW_PASSWORD not in rendered


@pytest.mark.parametrize(
    ("operation", "remove_field", "old_password", "new_password"),
    [
        ("MODIFY", "old_password", OLD_PASSWORD, NEW_PASSWORD),
        ("MODIFY", None, None, NEW_PASSWORD),
        ("MODIFY", None, "", NEW_PASSWORD),
        ("RESET", None, OLD_PASSWORD, NEW_PASSWORD),
        ("RESET", None, None, ""),
        ("RESET", "new_password", None, NEW_PASSWORD),
    ],
)
async def test_invalid_password_fields_use_stable_wire_code(
    operation: str,
    remove_field: str | None,
    old_password: str | None,
    new_password: str,
) -> None:
    runtime = SimpleNamespace(
        phase=RuntimePhase.READY,
        error_code=None,
        application_state=SimpleNamespace(config_service=object()),
    )
    body = _password_body(
        operation=operation,
        old_password=old_password,
        new_password=new_password,
    )
    if remove_field is not None:
        del body[remove_field]

    async with AsyncClient(
        transport=ASGITransport(app=create_management_app(runtime, _settings())),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/internal/v1/accounts/admin/password",
            json=body,
        )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "INVALID_ACCOUNT_PASSWORD"


def test_validation_exception_repr_hides_password_fields() -> None:
    from server.management.account_types import ManagedAccountPasswordEnvelope

    with pytest.raises(ValidationError) as captured:
        ManagedAccountPasswordEnvelope.model_validate(
            _password_body(
                operation="RESET",
                old_password=OLD_PASSWORD,
            )
        )

    rendered = repr(captured.value)
    assert OLD_PASSWORD not in rendered
    assert NEW_PASSWORD not in rendered


def test_password_request_rejects_extra_fields() -> None:
    from server.management.account_types import ManagedAccountPasswordEnvelope

    body = _password_body()
    body["account_name"] = "admin"

    with pytest.raises(ValidationError):
        ManagedAccountPasswordEnvelope.model_validate(body)


async def test_validation_response_and_logs_do_not_retain_passwords(
    caplog,
) -> None:
    caplog.set_level(logging.INFO)
    runtime = SimpleNamespace(
        phase=RuntimePhase.READY,
        error_code=None,
        application_state=SimpleNamespace(config_service=object()),
    )
    body = _password_body(
        operation="MODIFY",
        old_password=OLD_PASSWORD,
    )
    body["unexpected"] = NEW_PASSWORD

    async with AsyncClient(
        transport=ASGITransport(app=create_management_app(runtime, _settings())),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/internal/v1/accounts/admin/password",
            json=body,
        )

    assert response.status_code == 400
    assert OLD_PASSWORD not in response.text
    assert NEW_PASSWORD not in response.text
    assert OLD_PASSWORD not in caplog.text
    assert NEW_PASSWORD not in caplog.text


async def test_describe_returns_only_live_fixed_admin_when_bound(
    described_admin_context,
) -> None:
    factory, repository, service = described_admin_context
    await repository.bind_managed_identity(
        instance_id="pmcp-a",
        generation=1,
    )
    runtime = SimpleNamespace(
        phase=RuntimePhase.READY,
        error_code=None,
        application_state=SimpleNamespace(config_service=service),
    )

    async with AsyncClient(
        transport=ASGITransport(app=create_management_app(runtime, _settings())),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/api/internal/v1/accounts",
            params={
                "instance_id": "pmcp-a",
                "generation": 1,
                "account_name": "admin",
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "account_name": "admin",
        "account_status": "ACTIVE",
        "password_status": "RESET_REQUIRED",
    }
    async with factory() as session:
        binding_count = await session.scalar(select(func.count()).select_from(ManagedInstanceBinding))
    assert binding_count == 1


async def test_describe_rejects_unbound_identity_without_binding_it(
    described_admin_context,
) -> None:
    factory, _, service = described_admin_context
    runtime = SimpleNamespace(
        phase=RuntimePhase.READY,
        error_code=None,
        application_state=SimpleNamespace(config_service=service),
    )

    async with AsyncClient(
        transport=ASGITransport(app=create_management_app(runtime, _settings())),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/api/internal/v1/accounts",
            params={"instance_id": "pmcp-a", "generation": 1},
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "INSTANCE_IDENTITY_MISMATCH"
    async with factory() as session:
        binding_count = await session.scalar(select(func.count()).select_from(ManagedInstanceBinding))
    assert binding_count == 0


async def test_mutation_rejects_unbound_identity_without_binding_or_receipt(
    described_admin_context,
) -> None:
    from server.management.identity import ManagedIdentityError
    from server.models import ConfigOperationReceipt

    factory, _, config_service = described_admin_context

    with pytest.raises(ManagedIdentityError) as captured:
        await _account_service(config_service).mutate_admin_password(
            _password_envelope(),
            lease_owner="node-a",
        )

    assert captured.value.code == "INSTANCE_IDENTITY_MISMATCH"
    async with factory() as session:
        binding_count = await session.scalar(select(func.count()).select_from(ManagedInstanceBinding))
        receipt_count = await session.scalar(select(func.count()).select_from(ConfigOperationReceipt))
    assert binding_count == 0
    assert receipt_count == 0


async def test_describe_rejects_non_admin_with_sanitized_error(
    described_admin_context,
) -> None:
    _, repository, service = described_admin_context
    await repository.bind_managed_identity(
        instance_id="pmcp-a",
        generation=1,
    )
    runtime = SimpleNamespace(
        phase=RuntimePhase.READY,
        error_code=None,
        application_state=SimpleNamespace(config_service=service),
    )

    async with AsyncClient(
        transport=ASGITransport(app=create_management_app(runtime, _settings())),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/api/internal/v1/accounts",
            params={
                "instance_id": "pmcp-a",
                "generation": 1,
                "account_name": "not-an-account",
            },
        )

    assert response.status_code == 404
    assert response.json() == {
        "detail": {
            "code": "ACCOUNT_NOT_FOUND",
            "message": "Managed account operation failed",
        }
    }


def test_fixed_admin_identity_requires_exact_lowercase() -> None:
    from server.management.accounts import is_exact_managed_admin

    lowercase = User(
        external_id="admin",
        display_name="Administrator",
        auth_provider=AuthProvider.BUILTIN,
        role=UserRole.ADMIN,
        status=UserStatus.ACTIVE,
    )
    mixed_case = User(
        external_id="Admin",
        display_name="Administrator",
        auth_provider=AuthProvider.BUILTIN,
        role=UserRole.ADMIN,
        status=UserStatus.ACTIVE,
    )

    assert is_exact_managed_admin(lowercase)
    assert not is_exact_managed_admin(mixed_case)


async def test_first_reset_activates_fixed_admin(
    reset_required_account_context,
) -> None:
    factory, _, config_service = reset_required_account_context

    result = await _account_service(config_service).mutate_admin_password(
        _password_envelope(),
        lease_owner="node-a",
    )

    assert result.model_dump(mode="json") == {
        "account_name": "admin",
        "account_status": "ACTIVE",
        "password_status": "ACTIVE",
    }
    async with factory() as session:
        user = await session.scalar(select(User).where(User.external_id == "admin"))
        assert user is not None
        assert user.credential_epoch == 2
        assert await authenticate_builtin(session, "admin", NEW_PASSWORD) is not None


async def test_reset_can_replace_an_active_password(
    reset_required_account_context,
) -> None:
    factory, _, config_service = reset_required_account_context
    account_service = _account_service(config_service)
    await account_service.mutate_admin_password(
        _password_envelope(idempotency_key="first-reset"),
        lease_owner="node-a",
    )

    result = await account_service.mutate_admin_password(
        _password_envelope(
            new_password="replacement-managed-password",
            idempotency_key="active-reset",
        ),
        lease_owner="node-a",
    )

    assert result.password_status == PasswordState.ACTIVE
    async with factory() as session:
        assert (
            await authenticate_builtin(
                session,
                "admin",
                "replacement-managed-password",
            )
            is not None
        )


async def test_modify_requires_the_current_active_password(
    reset_required_account_context,
) -> None:
    factory, _, config_service = reset_required_account_context
    account_service = _account_service(config_service)
    await account_service.mutate_admin_password(
        _password_envelope(idempotency_key="activate-account"),
        lease_owner="node-a",
    )

    result = await account_service.mutate_admin_password(
        _password_envelope(
            operation="MODIFY",
            old_password=NEW_PASSWORD,
            new_password="modified-managed-password",
            idempotency_key="modify-account",
        ),
        lease_owner="node-a",
    )

    assert result.password_status == PasswordState.ACTIVE
    async with factory() as session:
        assert (
            await authenticate_builtin(
                session,
                "admin",
                "modified-managed-password",
            )
            is not None
        )


async def test_wrong_old_password_is_semantic_and_abandons_claim(
    reset_required_account_context,
) -> None:
    from server.management.accounts import ManagedAccountError

    _, repository, config_service = reset_required_account_context
    account_service = _account_service(config_service)
    await account_service.mutate_admin_password(
        _password_envelope(idempotency_key="activate-account"),
        lease_owner="node-a",
    )
    wrong = _password_envelope(
        operation="MODIFY",
        old_password="incorrect-managed-password",
        new_password="modified-managed-password",
        idempotency_key="retryable-semantic-failure",
    )

    with pytest.raises(ManagedAccountError) as captured:
        await account_service.mutate_admin_password(
            wrong,
            lease_owner="node-a",
        )

    assert captured.value.code == "OLD_PASSWORD_INCORRECT"
    retry = wrong.model_copy(update={"old_password": NEW_PASSWORD})
    result = await account_service.mutate_admin_password(
        retry,
        lease_owner="node-b",
    )
    assert result.password_status == PasswordState.ACTIVE
    key_hash = config_service.crypto.managed_digest(
        "idempotency-key-v1",
        "retryable-semantic-failure",
    )
    receipt = await repository.get_receipt(
        actor_scope="control:pmcp-a:1",
        idempotency_key_hash=key_hash,
    )
    assert receipt is not None
    assert receipt.status == "SUCCEEDED"


async def test_same_key_replays_without_a_second_password_mutation(
    reset_required_account_context,
) -> None:
    factory, _, config_service = reset_required_account_context
    account_service = _account_service(config_service)
    envelope = _password_envelope()

    first = await account_service.mutate_admin_password(
        envelope,
        lease_owner="node-a",
    )
    replay = await account_service.mutate_admin_password(
        envelope,
        lease_owner="node-b",
    )

    assert replay == first
    async with factory() as session:
        user = await session.scalar(select(User).where(User.external_id == "admin"))
        assert user is not None
        assert user.credential_epoch == 2


async def test_same_key_with_different_password_conflicts(
    reset_required_account_context,
) -> None:
    from server.management.accounts import ManagedAccountError

    _, _, config_service = reset_required_account_context
    account_service = _account_service(config_service)
    await account_service.mutate_admin_password(
        _password_envelope(),
        lease_owner="node-a",
    )

    with pytest.raises(ManagedAccountError) as captured:
        await account_service.mutate_admin_password(
            _password_envelope(new_password="different-managed-password"),
            lease_owner="node-b",
        )

    assert captured.value.code == "IDEMPOTENCY_CONFLICT"


async def test_second_service_replays_committed_response_after_response_loss(
    reset_required_account_context,
) -> None:
    factory, _, config_service = reset_required_account_context
    envelope = _password_envelope()
    first_node = _account_service(config_service)
    second_node = _account_service(ConfigService(config_service.repository, config_service.crypto))

    committed = await first_node.mutate_admin_password(
        envelope,
        lease_owner="node-a",
    )
    replay = await second_node.mutate_admin_password(
        envelope,
        lease_owner="node-b",
    )

    assert replay == committed
    async with factory() as session:
        user = await session.scalar(select(User).where(User.external_id == "admin"))
        assert user is not None
        assert user.credential_epoch == 2


async def test_generation_mismatch_fails_without_password_leak(
    reset_required_account_context,
) -> None:
    from server.management.identity import ManagedIdentityError

    _, _, config_service = reset_required_account_context

    with pytest.raises(ManagedIdentityError) as captured:
        await _account_service(config_service).mutate_admin_password(
            _password_envelope(generation=2),
            lease_owner="node-a",
        )

    assert captured.value.code == "INSTANCE_GENERATION_MISMATCH"
    assert NEW_PASSWORD not in str(captured.value)


async def test_concurrent_distinct_modifies_do_not_overwrite_stale_state(
    reset_required_account_context,
) -> None:
    from server.management.accounts import ManagedAccountError
    from server.management.account_types import ManagedAccountMutationResult

    factory, _, config_service = reset_required_account_context
    account_service = _account_service(config_service)
    await account_service.mutate_admin_password(
        _password_envelope(idempotency_key="activate-account"),
        lease_owner="node-a",
    )
    candidates = ("concurrent-password-a", "concurrent-password-b")

    async def mutate(candidate: str):
        try:
            return await account_service.mutate_admin_password(
                _password_envelope(
                    operation="MODIFY",
                    old_password=NEW_PASSWORD,
                    new_password=candidate,
                    idempotency_key=f"modify-{candidate}",
                ),
                lease_owner=f"node-{candidate[-1]}",
            )
        except ManagedAccountError as error:
            return error

    results = await asyncio.gather(*(mutate(value) for value in candidates))

    assert sum(isinstance(item, ManagedAccountMutationResult) for item in results) == 1
    assert [item.code for item in results if isinstance(item, ManagedAccountError)] == ["OLD_PASSWORD_INCORRECT"]
    async with factory() as session:
        authenticated = [
            candidate for candidate in candidates if await authenticate_builtin(session, "admin", candidate) is not None
        ]
    assert len(authenticated) == 1
