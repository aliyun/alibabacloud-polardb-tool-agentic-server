from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.ext.asyncio import async_sessionmaker

from server.api.departments import DepartmentResponse, UpdateDepartmentRequest
from server.api.users import CreateUserRequest, UpdateUserRequest, UserResponse
from server.app import create_app
from server.mcp.tools import resolve_target_instance
from server.models import AuthProvider, Base, User


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as value:
        yield value
    await engine.dispose()


async def test_unassigned_user_never_starts_background_provisioning(session):
    user = User(
        external_id="unassigned-user",
        display_name="Unassigned User",
        auth_provider=AuthProvider.BUILTIN,
    )
    session.add(user)
    await session.commit()

    background_tasks: set = set()
    result = await resolve_target_instance(
        user,
        session,
        session_factory=object(),
        background_tasks=background_tasks,
    )

    assert isinstance(result, dict)
    payload = json.loads(result["content"][0]["text"])
    assert payload == {
        "error": "NO_INSTANCE_ASSIGNED",
        "message": (
            "No administrator-assigned instance is available. "
            "Contact an administrator to register and assign an instance."
        ),
    }
    assert background_tasks == set()


def test_personal_provisioning_runtime_is_absent():
    root = Path(__file__).parents[1]
    retired = (
        "server/core/orchestrator.py",
        "server/core/pool_manager.py",
        "server/core/provisioner.py",
        "server/core/quota_manager.py",
        "server/core/provisioning/context.py",
        "server/core/provisioning/runner.py",
        "server/core/provisioning/states.py",
    )
    assert [path for path in retired if (root / path).exists()] == []

    app_source = (root / "server/app.py").read_text()
    assert "startup_recovery_sweep" not in app_source
    assert "replenishment_loop" not in app_source
    assert "health_check_loop" not in app_source


def test_polardb_provisioning_helpers_are_runtime_neutral():
    root = Path(__file__).parents[1]
    helper_source = (
        root / "server/core/polardb_provisioning_helpers.py"
    ).read_text()
    for retired_dependency in (
        "quota_manager",
        "pool_manager",
        "orchestrator",
        "server.core.provisioning",
    ):
        assert retired_dependency not in helper_source


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/pool/status"),
        ("get", "/api/pool/instances"),
        ("post", "/api/pool/instances"),
        ("delete", "/api/pool/instances/{instance_id}"),
        ("post", "/api/pool/replenish"),
        ("get", "/api/quota/status"),
        ("post", "/api/instances/{instance_id}/retry-provision"),
        ("delete", "/api/instances/{instance_id}/failed"),
    ],
)
def test_retired_routes_are_absent(method, path):
    matching_routes = [
        route
        for route in create_app().routes
        if getattr(route, "path", None) == path
        and method.upper() in (getattr(route, "methods", None) or set())
    ]
    assert matching_routes == []


def test_human_user_and_department_schemas_omit_retired_fields():
    for schema in (UserResponse, CreateUserRequest, UpdateUserRequest):
        assert "provisioning_mode" not in schema.model_fields
    for schema in (DepartmentResponse, UpdateDepartmentRequest):
        assert "max_instances" not in schema.model_fields
