from __future__ import annotations

from typing import Any

from sqlalchemy import and_
from sqlalchemy.sql.elements import ColumnElement

from server.models import (
    CredentialCapability,
    CredentialPurpose,
    CredentialStatus,
    InstanceCredential,
)

_DIRECT_CAPABILITIES = (
    CredentialCapability.READONLY,
    CredentialCapability.READWRITE,
)


def is_valid_direct_access_credential(
    credential: InstanceCredential | None,
    instance_id: str,
    *,
    require_database_name: bool = False,
) -> bool:
    return (
        credential is not None
        and credential.instance_id == instance_id
        and credential.resource_id is None
        and credential.purpose == CredentialPurpose.DIRECT_ACCESS
        and credential.status == CredentialStatus.ACTIVE
        and credential.capability in _DIRECT_CAPABILITIES
        and bool(credential.username_ciphertext)
        and bool(credential.password_ciphertext)
        and (
            not require_database_name
            or bool(credential.database_name)
        )
    )


def direct_access_credential_sql_predicate(
    instance_id: Any,
    *,
    require_database_name: bool = False,
) -> ColumnElement[bool]:
    clauses = [
        InstanceCredential.instance_id == instance_id,
        InstanceCredential.resource_id.is_(None),
        InstanceCredential.purpose == CredentialPurpose.DIRECT_ACCESS,
        InstanceCredential.status == CredentialStatus.ACTIVE,
        InstanceCredential.capability.in_(_DIRECT_CAPABILITIES),
        InstanceCredential.username_ciphertext.is_not(None),
        InstanceCredential.username_ciphertext != "",
        InstanceCredential.password_ciphertext.is_not(None),
        InstanceCredential.password_ciphertext != "",
    ]
    if require_database_name:
        clauses.extend(
            [
                InstanceCredential.database_name.is_not(None),
                InstanceCredential.database_name != "",
            ]
        )
    return and_(*clauses)
