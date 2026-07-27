from __future__ import annotations

from dataclasses import dataclass

from server.core.crypto import decrypt
from server.models import (
    CredentialCapability,
    CredentialPurpose,
    CredentialStatus,
    DBInstanceResource,
    DBInstanceStatus,
    InstanceCredential,
)

_DELETE_CAPABLE_STATUSES = frozenset(
    {
        DBInstanceStatus.CREATING,
        DBInstanceStatus.READY,
        DBInstanceStatus.FAILED,
        DBInstanceStatus.DELETING,
        DBInstanceStatus.DELETE_FAILED,
    }
)


@dataclass(frozen=True)
class ResourceConnectionDetails:
    database: str
    username: str
    password: str


def usable_resource_access_credential(
    resource: DBInstanceResource,
) -> InstanceCredential | None:
    candidates = [
        credential
        for credential in resource.credentials
        if credential.purpose == CredentialPurpose.RESOURCE_ACCESS
    ]
    if len(candidates) != 1:
        return None
    credential = candidates[0]
    if (
        credential.resource_id != resource.id
        or credential.instance_id is not None
        or credential.status != CredentialStatus.ACTIVE
        or credential.capability != CredentialCapability.READWRITE
        or credential.version != 1
        or not credential.username_ciphertext
        or not credential.password_ciphertext
        or not resource.database_name
        or credential.database_name != resource.database_name
    ):
        return None
    return credential


def resource_connection_details(
    resource: DBInstanceResource,
) -> ResourceConnectionDetails | None:
    credential = usable_resource_access_credential(resource)
    if credential is None or credential.database_name is None:
        return None
    username_ciphertext = credential.username_ciphertext
    password_ciphertext = credential.password_ciphertext
    if username_ciphertext is None or password_ciphertext is None:
        return None
    try:
        username = decrypt(username_ciphertext)
        password = decrypt(password_ciphertext)
    except Exception:
        return None
    return ResourceConnectionDetails(
        database=credential.database_name,
        username=username,
        password=password,
    )


def resource_capabilities(
    resource: DBInstanceResource,
    *,
    credentials_revealable: bool | None = None,
) -> tuple[str, ...]:
    capabilities = ["list", "describe"]
    if resource.status == DBInstanceStatus.READY:
        if credentials_revealable is None:
            credentials_revealable = (
                resource_connection_details(resource) is not None
            )
    if (
        resource.status == DBInstanceStatus.READY
        and credentials_revealable
    ):
        capabilities.append("credentials_read")
    if resource.status in _DELETE_CAPABLE_STATUSES:
        capabilities.append("delete")
    if (
        resource.status == DBInstanceStatus.READY
        and credentials_revealable
    ):
        capabilities.extend(("run_sql_read", "run_sql_write"))
    return tuple(capabilities)
