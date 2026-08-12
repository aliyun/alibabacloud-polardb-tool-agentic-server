from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from server.configuration.registry import MODULE_REGISTRY
from server.configuration.secrets import (
    SecretFieldSpec,
    decrypt_secret_tree,
    encrypt_stored_secret,
)
from server.configuration.types import ModuleDocument, ModuleState
from server.core.config_crypto import ConfigCrypto


LEGACY_ALIYUN_ACCESS_SECRET_FIELDS = (
    SecretFieldSpec("access_key_id", display_mask=True),
    SecretFieldSpec("access_key_secret"),
)


class ModuleMigrationError(ValueError):
    pass


def secret_fields_for_schema(
    module: str, schema_version: int
) -> tuple[SecretFieldSpec, ...]:
    if module == "aliyun_access":
        if schema_version == 1:
            return LEGACY_ALIYUN_ACCESS_SECRET_FIELDS
        if schema_version == 2:
            return MODULE_REGISTRY[module].secret_fields
    elif schema_version == MODULE_REGISTRY[module].schema_version:
        return MODULE_REGISTRY[module].secret_fields
    raise ModuleMigrationError(
        f"Unsupported {module} schema version {schema_version}"
    )


def secret_fields_for_document(
    module: str, document: ModuleDocument
) -> tuple[SecretFieldSpec, ...]:
    return secret_fields_for_schema(module, document.schema_version)


def _as_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _legacy_aliyun_plaintext(
    config: dict[str, Any], crypto: ConfigCrypto
) -> dict[str, Any]:
    return decrypt_secret_tree(
        config,
        secret_fields=secret_fields_for_schema("aliyun_access", 1),
        crypto=crypto,
        module="aliyun_access",
        schema_version=1,
    )


def _required_string(config: dict[str, Any], field: str) -> str:
    value = config.get(field)
    if not isinstance(value, str) or not value:
        raise ModuleMigrationError(
            f"Legacy aliyun_access {field} is missing or invalid"
        )
    return value


def _v2_secret(
    plaintext: str,
    *,
    path: str,
    crypto: ConfigCrypto,
    migrated_at: datetime,
) -> dict[str, Any]:
    return encrypt_stored_secret(
        plaintext,
        spec=SecretFieldSpec(path, display_mask=path.endswith("key_id")),
        crypto=crypto,
        module="aliyun_access",
        schema_version=2,
        now=migrated_at,
    )


def _migrate_aliyun_config(
    config: dict[str, Any],
    *,
    crypto: ConfigCrypto,
    legacy_updated_at: datetime,
) -> dict[str, Any]:
    try:
        plaintext = _legacy_aliyun_plaintext(config, crypto)
        mode = plaintext.get("credential_mode", "direct_ak")
        if mode not in {"direct_ak", "assume_role"}:
            raise ModuleMigrationError("Legacy credential mode is invalid")
        result: dict[str, Any] = {
            "credential_mode": mode,
            "region_id": str(plaintext.get("region_id") or "cn-hangzhou"),
            "openapi_network": str(
                plaintext.get("openapi_network") or "public"
            ),
        }
        access_key_id = _required_string(plaintext, "access_key_id")
        access_key_secret = _required_string(plaintext, "access_key_secret")
        migrated_at = _as_aware(legacy_updated_at)
        if mode == "direct_ak":
            result["direct_ak"] = {
                "access_key_id": _v2_secret(
                    access_key_id,
                    path="direct_ak.access_key_id",
                    crypto=crypto,
                    migrated_at=migrated_at,
                ),
                "access_key_secret": _v2_secret(
                    access_key_secret,
                    path="direct_ak.access_key_secret",
                    crypto=crypto,
                    migrated_at=migrated_at,
                ),
            }
            return result
        result["assume_role"] = {
            "source_access_key_id": _v2_secret(
                access_key_id,
                path="assume_role.source_access_key_id",
                crypto=crypto,
                migrated_at=migrated_at,
            ),
            "source_access_key_secret": _v2_secret(
                access_key_secret,
                path="assume_role.source_access_key_secret",
                crypto=crypto,
                migrated_at=migrated_at,
            ),
            "role_arn": _required_string(plaintext, "role_arn"),
            "role_session_name": str(
                plaintext.get("role_session_name") or "polardb-agentic"
            ),
            "duration_seconds": int(
                plaintext.get("sts_duration_seconds", 3600)
            ),
        }
        return result
    except ModuleMigrationError:
        raise
    except Exception as exc:
        raise ModuleMigrationError(
            "Unable to decrypt legacy aliyun_access configuration"
        ) from exc


def migrate_aliyun_access_v1_to_v2(
    document: ModuleDocument,
    *,
    crypto: ConfigCrypto,
    legacy_updated_at: datetime,
) -> ModuleDocument:
    updates: dict[str, Any] = {"schema_version": 2, "last_validation": None}
    if document.draft is not None:
        updates["draft"] = _migrate_aliyun_config(
            document.draft,
            crypto=crypto,
            legacy_updated_at=legacy_updated_at,
        )
    if document.effective is not None:
        updates["effective"] = document.effective.model_copy(
            update={
                "config": _migrate_aliyun_config(
                    document.effective.config,
                    crypto=crypto,
                    legacy_updated_at=legacy_updated_at,
                )
            }
        )
    return document.model_copy(update=updates)


Migration = Callable[..., ModuleDocument]
MIGRATIONS: dict[tuple[str, int, int], Migration] = {
    ("aliyun_access", 1, 2): migrate_aliyun_access_v1_to_v2,
}


def migrate_document_for_write(
    module: str,
    document: ModuleDocument,
    *,
    crypto: ConfigCrypto,
    legacy_updated_at: datetime,
) -> ModuleDocument:
    target = MODULE_REGISTRY[module].schema_version
    secret_fields_for_document(module, document)
    current = document
    while current.schema_version < target:
        migration = MIGRATIONS.get(
            (module, current.schema_version, current.schema_version + 1)
        )
        if migration is None:
            raise ModuleMigrationError(
                f"No migration is available for {module} schema version "
                f"{current.schema_version}"
            )
        current = migration(
            current,
            crypto=crypto,
            legacy_updated_at=legacy_updated_at,
        )
    return current


def _project_v1_aliyun_config(
    config: dict[str, Any], crypto: ConfigCrypto
) -> dict[str, Any]:
    plaintext = _legacy_aliyun_plaintext(config, crypto)
    mode = plaintext.get("credential_mode", "direct_ak")
    common = {
        "credential_mode": mode,
        "region_id": plaintext.get("region_id", "cn-hangzhou"),
        "openapi_network": plaintext.get("openapi_network", "public"),
    }
    if mode == "direct_ak":
        return {
            **common,
            "direct_ak": {
                "access_key_id": plaintext.get("access_key_id"),
                "access_key_secret": plaintext.get("access_key_secret"),
            },
        }
    if mode == "assume_role":
        return {
            **common,
            "assume_role": {
                "source_access_key_id": plaintext.get("access_key_id"),
                "source_access_key_secret": plaintext.get("access_key_secret"),
                "role_arn": plaintext.get("role_arn"),
                "role_session_name": plaintext.get(
                    "role_session_name", "polardb-agentic"
                ),
                "duration_seconds": plaintext.get("sts_duration_seconds", 3600),
            },
        }
    raise ModuleMigrationError("Legacy credential mode is invalid")


def decrypt_effective_config(
    module: str,
    document: ModuleDocument,
    crypto: ConfigCrypto,
    *,
    active_only: bool = True,
) -> dict[str, Any]:
    secret_fields_for_document(module, document)
    if (
        document.effective is None
        or document.effective.state != ModuleState.ACTIVE
    ):
        return {}
    config = document.effective.config
    if module != "aliyun_access":
        return decrypt_secret_tree(
            config,
            secret_fields=secret_fields_for_document(module, document),
            crypto=crypto,
            module=module,
            schema_version=document.schema_version,
        )
    if document.schema_version == 1:
        try:
            return _project_v1_aliyun_config(config, crypto)
        except ModuleMigrationError:
            raise
        except Exception as exc:
            raise ModuleMigrationError(
                "Unable to decrypt legacy aliyun_access configuration"
            ) from exc
    secret_fields = secret_fields_for_document(module, document)
    if active_only:
        mode = config.get("credential_mode")
        if not isinstance(mode, str):
            # Draft/plan candidates can be schema-invalid by design. Leave
            # them untouched so the normal module validator reports that
            # error without decrypting an indeterminate credential block.
            return dict(config)
        secret_fields = tuple(
            field
            for field in secret_fields
            if field.path.startswith(f"{mode}.")
        )
    decrypted = decrypt_secret_tree(
        config,
        secret_fields=secret_fields,
        crypto=crypto,
        module=module,
        schema_version=document.schema_version,
    )
    if active_only:
        for credential_mode in ("direct_ak", "assume_role", "ecs_ram_role"):
            if credential_mode != mode:
                decrypted.pop(credential_mode, None)
    return decrypted
