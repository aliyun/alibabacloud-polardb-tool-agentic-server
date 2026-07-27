from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from server.core.binding_manager import UserSQLCredential
from server.mcp.tools.handlers import _resolve_user_sql_credential
from server.models import (
    AuditStatus,
    InstanceTopology,
    Permission,
    TenantProvisioningStep,
)


def _session() -> MagicMock:
    session = MagicMock(spec=AsyncSession)
    session.in_transaction.return_value = True
    session.rollback = AsyncMock()
    return session


def _resolved_incomplete() -> UserSQLCredential:
    return UserSQLCredential(
        binding=SimpleNamespace(
            provisioning_step=TenantProvisioningStep.PENDING
        ),
        credential=SimpleNamespace(),
        permission=Permission.READWRITE,
    )


@pytest.mark.parametrize(
    ("phase", "topology", "existing", "expected_code", "expected_message"),
    [
        (
            "tenant_create",
            InstanceTopology.MULTITENANT,
            None,
            "TENANT_PROVISION_FAILED",
            "Tenant provisioning failed.",
        ),
        (
            "direct_account",
            InstanceTopology.SINGLE_TENANT,
            None,
            "CONNECTION_ERROR",
            "Database account provisioning failed.",
        ),
        (
            "tenant_resume",
            InstanceTopology.MULTITENANT,
            _resolved_incomplete(),
            "TENANT_PROVISION_FAILED",
            "Tenant provisioning failed.",
        ),
    ],
)
async def test_all_provisioning_phases_return_only_stable_errors(
    phase,
    topology,
    existing,
    expected_code,
    expected_message,
):
    sentinel = (
        "password=SECRET host=private db=secret_db "
        "SQL=CREATE USER SENTINEL"
    )
    user = SimpleNamespace(id="user-id")
    instance = SimpleNamespace(id="instance-id", topology=topology)
    session = _session()
    access = SimpleNamespace(permission=Permission.READWRITE)

    with (
        patch(
            "server.mcp.tools.handlers.resolve_user_instance_access",
            new_callable=AsyncMock,
            return_value=access,
        ),
        patch(
            "server.mcp.tools.handlers.get_user_credential",
            new_callable=AsyncMock,
            return_value=existing,
        ),
        patch(
            "server.mcp.tools.handlers.create_db_account",
            new_callable=AsyncMock,
            side_effect=(
                RuntimeError(sentinel)
                if phase == "direct_account"
                else None
            ),
        ),
        patch(
            "server.core.tenant_provisioner.ensure_tenant",
            new_callable=AsyncMock,
            side_effect=(
                RuntimeError(sentinel)
                if phase != "direct_account"
                else None
            ),
        ),
        patch(
            "server.mcp.tools.handlers.log_audit",
            new_callable=AsyncMock,
        ) as audit,
    ):
        result = await _resolve_user_sql_credential(
            user, instance, session
        )

    payload = json.loads(result["content"][0]["text"])
    assert payload == {
        "error": expected_code,
        "message": expected_message,
    }
    session.rollback.assert_awaited_once()
    assert audit.await_args.kwargs["action"] == "sql_credential.resolve"
    assert audit.await_args.kwargs["status"] == AuditStatus.ERROR
    assert audit.await_args.kwargs["error_code"] == expected_code
    assert audit.await_args.kwargs["error_message"] == expected_message
    assert sentinel not in str(audit.await_args.kwargs)


@pytest.mark.parametrize(
    ("phase", "topology", "existing"),
    [
        ("tenant_create", InstanceTopology.MULTITENANT, None),
        ("direct_account", InstanceTopology.SINGLE_TENANT, None),
        (
            "tenant_resume",
            InstanceTopology.MULTITENANT,
            _resolved_incomplete(),
        ),
    ],
)
async def test_all_provisioning_phases_preserve_cancellation(
    phase,
    topology,
    existing,
):
    user = SimpleNamespace(id="user-id")
    instance = SimpleNamespace(id="instance-id", topology=topology)
    session = _session()
    access = SimpleNamespace(permission=Permission.READWRITE)

    with (
        patch(
            "server.mcp.tools.handlers.resolve_user_instance_access",
            new_callable=AsyncMock,
            return_value=access,
        ),
        patch(
            "server.mcp.tools.handlers.get_user_credential",
            new_callable=AsyncMock,
            return_value=existing,
        ),
        patch(
            "server.mcp.tools.handlers.create_db_account",
            new_callable=AsyncMock,
            side_effect=(
                asyncio.CancelledError
                if phase == "direct_account"
                else None
            ),
        ),
        patch(
            "server.core.tenant_provisioner.ensure_tenant",
            new_callable=AsyncMock,
            side_effect=(
                asyncio.CancelledError
                if phase != "direct_account"
                else None
            ),
        ),
        patch(
            "server.mcp.tools.handlers.log_audit",
            new_callable=AsyncMock,
        ) as audit,
        pytest.raises(asyncio.CancelledError),
    ):
        await _resolve_user_sql_credential(user, instance, session)

    session.rollback.assert_not_awaited()
    audit.assert_not_awaited()


@pytest.mark.parametrize("lookup", ["access", "credential"])
async def test_prerequisite_lookup_errors_are_also_sanitized(lookup):
    sentinel = "password=SECRET lookup=SENTINEL"
    user = SimpleNamespace(id="user-id")
    instance = SimpleNamespace(
        id="instance-id",
        topology=InstanceTopology.SINGLE_TENANT,
    )
    session = _session()
    access = SimpleNamespace(permission=Permission.READWRITE)

    with (
        patch(
            "server.mcp.tools.handlers.resolve_user_instance_access",
            new_callable=AsyncMock,
            side_effect=(
                RuntimeError(sentinel) if lookup == "access" else None
            ),
            return_value=access,
        ),
        patch(
            "server.mcp.tools.handlers.get_user_credential",
            new_callable=AsyncMock,
            side_effect=(
                RuntimeError(sentinel)
                if lookup == "credential"
                else None
            ),
        ),
        patch(
            "server.mcp.tools.handlers.log_audit",
            new_callable=AsyncMock,
        ) as audit,
    ):
        result = await _resolve_user_sql_credential(
            user, instance, session
        )

    assert json.loads(result["content"][0]["text"]) == {
        "error": "CONNECTION_ERROR",
        "message": "Database credential resolution failed.",
    }
    assert sentinel not in str(audit.await_args.kwargs)
