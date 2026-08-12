from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from server.core.config_crypto import ConfigCrypto, SecretEnvelope


@dataclass(frozen=True, slots=True)
class SecretFieldSpec:
    path: str
    display_mask: bool = False


class SecretInputError(ValueError):
    pass


_MISSING = object()


def mask_access_key_id(value: str) -> str:
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}****{value[-4:]}"


def _value_at_path(value: dict[str, Any], path: str) -> Any:
    current: Any = value
    for segment in path.split("."):
        if not isinstance(current, dict) or segment not in current:
            return _MISSING
        current = current[segment]
    return current


def _set_at_path(value: dict[str, Any], path: str, replacement: Any) -> None:
    *parents, leaf = path.split(".")
    current = value
    for segment in parents:
        child = current.get(segment)
        if not isinstance(child, dict):
            child = {}
            current[segment] = child
        current = child
    current[leaf] = replacement


def _remove_at_path(
    value: dict[str, Any], path: str, *, prune_empty: bool = False
) -> None:
    parents: list[tuple[dict[str, Any], str]] = []
    current: Any = value
    for segment in path.split("."):
        if not isinstance(current, dict) or segment not in current:
            return
        parents.append((current, segment))
        current = current[segment]
    parent, leaf = parents.pop()
    parent.pop(leaf, None)
    if prune_empty:
        for parent, segment in reversed(parents):
            child = parent.get(segment)
            if child != {}:
                return
            parent.pop(segment, None)


def encrypt_stored_secret(
    plaintext: str,
    *,
    spec: SecretFieldSpec,
    crypto: ConfigCrypto,
    module: str,
    schema_version: int,
    now: datetime,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "$secret": crypto.encrypt_field(
            plaintext,
            module=module,
            field_path=spec.path,
            schema_version=schema_version,
        ).model_dump(mode="json"),
        "updated_at": now.isoformat(),
    }
    if spec.display_mask:
        value["display_hint"] = mask_access_key_id(plaintext)
    return value


def merge_secret_tree(
    base: dict[str, Any],
    incoming: dict[str, Any],
    *,
    secret_fields: tuple[SecretFieldSpec, ...],
    crypto: ConfigCrypto,
    module: str,
    schema_version: int,
    now: datetime,
) -> dict[str, Any]:
    """Merge an input document without allowing declared secrets to escape."""
    result = deepcopy(base)
    for field, value in incoming.items():
        result[field] = deepcopy(value)

    for spec in secret_fields:
        incoming_value = _value_at_path(incoming, spec.path)
        if incoming_value is _MISSING:
            stored_value = _value_at_path(base, spec.path)
            if stored_value is not _MISSING:
                _set_at_path(result, spec.path, deepcopy(stored_value))
            continue
        if incoming_value == {"configured": True}:
            raise SecretInputError(
                f"{spec.path} placeholder cannot be used as input"
            )
        if incoming_value == {"$secret_action": "clear"}:
            _remove_at_path(result, spec.path)
            continue
        if not isinstance(incoming_value, str):
            raise SecretInputError(
                f"{spec.path} must be supplied as plaintext"
            )
        _set_at_path(
            result,
            spec.path,
            encrypt_stored_secret(
                incoming_value,
                spec=spec,
                crypto=crypto,
                module=module,
                schema_version=schema_version,
                now=now,
            ),
        )
    return result


def decrypt_secret_tree(
    config: dict[str, Any],
    *,
    secret_fields: tuple[SecretFieldSpec, ...],
    crypto: ConfigCrypto,
    module: str,
    schema_version: int,
) -> dict[str, Any]:
    result = deepcopy(config)
    for spec in secret_fields:
        stored = _value_at_path(result, spec.path)
        if isinstance(stored, dict) and "$secret" in stored:
            _set_at_path(
                result,
                spec.path,
                crypto.decrypt_field(
                    SecretEnvelope.model_validate(stored["$secret"]),
                    module=module,
                    field_path=spec.path,
                    schema_version=schema_version,
                ),
            )
    return result


def redact_secret_tree(
    config: dict[str, Any],
    *,
    secret_fields: tuple[SecretFieldSpec, ...],
) -> dict[str, Any]:
    result = deepcopy(config)
    for spec in secret_fields:
        stored = _value_at_path(result, spec.path)
        if stored is _MISSING:
            continue
        redacted: dict[str, Any] = {"configured": True}
        if isinstance(stored, dict):
            for field in ("updated_at", "display_hint"):
                if field in stored:
                    redacted[field] = stored[field]
        _set_at_path(result, spec.path, redacted)
    return result


def export_secret_tree(
    config: dict[str, Any],
    *,
    secret_fields: tuple[SecretFieldSpec, ...],
) -> dict[str, Any]:
    result = deepcopy(config)
    for spec in secret_fields:
        _remove_at_path(result, spec.path, prune_empty=True)
    return result


def configured_secret_fields(
    config: dict[str, Any],
    *,
    secret_fields: tuple[SecretFieldSpec, ...],
) -> list[str]:
    return [
        spec.path
        for spec in secret_fields
        if _value_at_path(config, spec.path) is not _MISSING
    ]
