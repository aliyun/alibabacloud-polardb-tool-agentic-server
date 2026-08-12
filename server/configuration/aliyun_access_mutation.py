from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from server.configuration.aliyun_access import (
    AliyunAccessMutation,
    CredentialMode,
    CredentialTransition,
    generate_role_session_name,
)
from server.configuration.secrets import (
    SecretInputError,
    SecretFieldSpec,
    decrypt_secret_tree,
    merge_secret_tree,
)
from server.core.config_crypto import ConfigCrypto


_CREDENTIAL_MODES: tuple[CredentialMode, ...] = (
    "direct_ak",
    "assume_role",
    "ecs_ram_role",
)
CREDENTIAL_BLOCK_KEYS = frozenset(_CREDENTIAL_MODES)
_DIRECT_AK_SECRET_FIELDS = (
    SecretFieldSpec("direct_ak.access_key_id", display_mask=True),
    SecretFieldSpec("direct_ak.access_key_secret"),
)
_ALIYUN_SECRET_FIELDS = (
    *_DIRECT_AK_SECRET_FIELDS,
    SecretFieldSpec("assume_role.source_access_key_id", display_mask=True),
    SecretFieldSpec("assume_role.source_access_key_secret"),
    SecretFieldSpec("assume_role.external_id"),
)


class AliyunTransitionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AliyunTransitionAudit:
    previous_mode: CredentialMode | None
    selected_mode: CredentialMode
    previous_mode_action: str
    selected_mode_action: str
    reused_direct_source: bool
    credential_id_mask: str | None
    delete_retained_modes: tuple[CredentialMode, ...]


def _stored_credential_id_mask(
    config: dict[str, Any], selected_mode: CredentialMode
) -> str | None:
    field = (
        "access_key_id"
        if selected_mode == "direct_ak"
        else "source_access_key_id"
        if selected_mode == "assume_role"
        else None
    )
    if field is None:
        return None
    stored = _value_at_path(config, f"{selected_mode}.{field}")
    if not isinstance(stored, dict):
        return None
    hint = stored.get("display_hint")
    if hint == "****":
        return hint
    if not isinstance(hint, str) or hint.count("****") != 1:
        return None
    prefix, suffix = hint.split("****")
    if (
        not 1 <= len(prefix) <= 4
        or len(suffix) != 4
        or not (prefix + suffix).isalnum()
    ):
        return None
    return hint


def _parse_transition(value: Any) -> CredentialTransition:
    try:
        return CredentialTransition.model_validate(value or {})
    except ValidationError as exc:
        raise AliyunTransitionError(
            "Credential transition input is invalid"
        ) from exc


def _direct_source_from(
    base: dict[str, Any],
    *,
    crypto: ConfigCrypto,
    schema_version: int,
) -> dict[str, str]:
    direct = base.get("direct_ak")
    if not isinstance(direct, dict):
        raise AliyunTransitionError(
            "A retained direct access key is required as the AssumeRole source"
        )
    decrypted = decrypt_secret_tree(
        {"direct_ak": direct},
        secret_fields=_DIRECT_AK_SECRET_FIELDS,
        crypto=crypto,
        module="aliyun_access",
        schema_version=schema_version,
    ).get("direct_ak")
    if not isinstance(decrypted, dict):
        raise AliyunTransitionError(
            "A retained direct access key is required as the AssumeRole source"
        )
    key_id = decrypted.get("access_key_id")
    key_secret = decrypted.get("access_key_secret")
    if not isinstance(key_id, str) or not isinstance(key_secret, str):
        raise AliyunTransitionError(
            "The retained direct access key is incomplete"
        )
    return {
        "source_access_key_id": key_id,
        "source_access_key_secret": key_secret,
    }


def _prepared_mutation_payload(
    base: dict[str, Any],
    incoming: dict[str, Any],
    *,
    transition: CredentialTransition,
    crypto: ConfigCrypto,
    schema_version: int,
    generate_session_name: bool,
) -> dict[str, Any]:
    payload = deepcopy(incoming)
    selected_mode = payload.get("credential_mode")
    if (
        transition.reuse_direct_ak_as_assume_source
        and selected_mode != "assume_role"
    ):
        raise AliyunTransitionError(
            "Direct access keys can only be reused as an AssumeRole source"
        )
    if transition.reuse_direct_ak_as_assume_source:
        if transition.selected_mode_action != "replace":
            raise AliyunTransitionError(
                "Direct source reuse requires replacing the AssumeRole block"
            )
        assume_role = payload.get("assume_role")
        if not isinstance(assume_role, dict):
            raise AliyunTransitionError(
                "An AssumeRole block is required when reusing a direct source"
            )
        payload["assume_role"] = {
            **assume_role,
            **_direct_source_from(
                base, crypto=crypto, schema_version=schema_version
            ),
        }

    if (
        selected_mode == "assume_role"
        and transition.selected_mode_action == "replace"
        and generate_session_name
    ):
        assume_role = payload.get("assume_role")
        if isinstance(assume_role, dict) and "role_session_name" not in assume_role:
            payload["assume_role"] = {
                **assume_role,
                "role_session_name": generate_role_session_name(),
            }
    return payload


def _value_at_path(value: dict[str, Any], path: str) -> Any:
    current: Any = value
    for segment in path.split("."):
        if not isinstance(current, dict) or segment not in current:
            return None
        current = current[segment]
    return current


def _reject_secret_placeholders(payload: dict[str, Any]) -> None:
    for spec in _ALIYUN_SECRET_FIELDS:
        value = _value_at_path(payload, spec.path)
        if isinstance(value, dict) and value.get("configured") is True:
            raise SecretInputError(
                f"{spec.path} placeholder cannot be used as input"
            )


def _decrypted_selected_block(
    base: dict[str, Any],
    selected_mode: CredentialMode,
    *,
    crypto: ConfigCrypto,
    schema_version: int,
) -> dict[str, Any]:
    selected = base.get(selected_mode)
    if not isinstance(selected, dict):
        raise AliyunTransitionError(
            "The selected credential block does not exist"
        )
    secret_fields = tuple(
        spec
        for spec in _ALIYUN_SECRET_FIELDS
        if spec.path.startswith(f"{selected_mode}.")
    )
    decrypted = decrypt_secret_tree(
        {selected_mode: selected},
        secret_fields=secret_fields,
        crypto=crypto,
        module="aliyun_access",
        schema_version=schema_version,
    ).get(selected_mode)
    if not isinstance(decrypted, dict):
        raise AliyunTransitionError(
            "The selected credential block does not exist"
        )
    return decrypted


def _selected_secret_fields(selected_mode: CredentialMode) -> tuple[str, ...]:
    prefix = f"{selected_mode}."
    return tuple(
        spec.path.removeprefix(prefix)
        for spec in _ALIYUN_SECRET_FIELDS
        if spec.path.startswith(prefix)
    )


def _merge_selected_partial_block(
    base: dict[str, Any],
    *,
    selected_mode: CredentialMode,
    raw_patch: dict[str, Any],
    completed_block: dict[str, Any],
    crypto: ConfigCrypto,
    schema_version: int,
    now: datetime,
) -> dict[str, Any]:
    result = merge_secret_tree(
        base,
        {selected_mode: raw_patch},
        secret_fields=_ALIYUN_SECRET_FIELDS,
        crypto=crypto,
        module="aliyun_access",
        schema_version=schema_version,
        now=now,
    )
    stored_block = result[selected_mode]
    assert isinstance(stored_block, dict)
    secret_fields = _selected_secret_fields(selected_mode)
    for field, value in completed_block.items():
        if field not in secret_fields:
            stored_block[field] = deepcopy(value)
    return result


def _validated_mutation(
    payload: dict[str, Any], transition: CredentialTransition
) -> AliyunAccessMutation:
    try:
        return AliyunAccessMutation.model_validate(
            {**payload, "transition": transition.model_dump(mode="python")}
        )
    except ValidationError as exc:
        raise AliyunTransitionError(
            "Selected credential configuration is invalid"
        ) from exc


def merge_aliyun_access_draft(
    base: dict[str, Any],
    incoming: dict[str, Any],
    *,
    crypto: ConfigCrypto,
    schema_version: int,
    now: datetime,
) -> tuple[dict[str, Any], AliyunTransitionAudit]:
    """Apply a credential-mode mutation without making retained blocks active."""
    payload = deepcopy(incoming)
    transition = _parse_transition(payload.pop("transition", None))
    previous_mode = base.get("credential_mode")
    if previous_mode is not None and previous_mode not in _CREDENTIAL_MODES:
        raise AliyunTransitionError("Stored credential mode is invalid")
    inferred_selected_mode = "credential_mode" not in payload
    if inferred_selected_mode and previous_mode is not None:
        payload["credential_mode"] = previous_mode
    requested_mode = payload.get("credential_mode")
    selected_is_partial_patch = (
        transition.selected_mode_action == "replace"
        and requested_mode == previous_mode
        and requested_mode in _CREDENTIAL_MODES
        and requested_mode in payload
    )
    payload = _prepared_mutation_payload(
        base,
        payload,
        transition=transition,
        crypto=crypto,
        schema_version=schema_version,
        generate_session_name=not selected_is_partial_patch,
    )
    _reject_secret_placeholders(payload)
    existing_selected_without_replacement = (
        requested_mode == previous_mode
        and requested_mode in _CREDENTIAL_MODES
        and requested_mode not in payload
    )
    validation_transition = transition
    if (
        existing_selected_without_replacement
        and transition.selected_mode_action == "replace"
    ):
        validation_transition = transition.model_copy(
            update={"selected_mode_action": "reuse_retained"}
        )
    validation_payload = payload
    if selected_is_partial_patch:
        selected_patch = payload.get(requested_mode)
        if not isinstance(selected_patch, dict):
            raise AliyunTransitionError(
                "The selected credential block must be an object"
            )
        completed_selected = {
            **_decrypted_selected_block(
                base,
                requested_mode,
                crypto=crypto,
                schema_version=schema_version,
            ),
            **selected_patch,
        }
        if completed_selected.get("external_id") == {"$secret_action": "clear"}:
            completed_selected["external_id"] = None
        validation_payload = {
            **payload,
            requested_mode: completed_selected,
        }
    mutation = _validated_mutation(validation_payload, validation_transition)
    selected_mode = mutation.credential_mode

    supplied_modes = {
        mode for mode in _CREDENTIAL_MODES if getattr(mutation, mode) is not None
    }
    if supplied_modes - {selected_mode}:
        raise AliyunTransitionError(
            "Only the selected credential block can be changed"
        )
    if selected_mode in transition.delete_retained_modes:
        raise AliyunTransitionError("The selected credential mode cannot be deleted")
    if (
        transition.selected_mode_action == "reuse_retained"
        and selected_mode in supplied_modes
    ):
        raise AliyunTransitionError(
            "A retained credential block cannot be replaced and reused together"
        )

    result = deepcopy(base)
    selected_is_reused = (
        transition.selected_mode_action == "reuse_retained"
        or existing_selected_without_replacement
    )
    if selected_is_partial_patch:
        completed_block = validation_payload[selected_mode]
        if not isinstance(completed_block, dict):
            raise AliyunTransitionError(
                "The selected credential block must be an object"
            )
        result = _merge_selected_partial_block(
            result,
            selected_mode=selected_mode,
            raw_patch=payload[selected_mode],
            completed_block=completed_block,
            crypto=crypto,
            schema_version=schema_version,
            now=now,
        )
    elif selected_is_reused:
        if not isinstance(result.get(selected_mode), dict):
            raise AliyunTransitionError(
                "The selected retained credential block does not exist"
            )
    else:
        selected_block = getattr(mutation, selected_mode)
        if selected_block is None:
            raise AliyunTransitionError(
                "The selected credential block is required"
            )
        result.pop(selected_mode, None)
        result = merge_secret_tree(
            result,
            {
                selected_mode: selected_block.model_dump(
                    mode="python", exclude_unset=True
                )
            },
            secret_fields=_ALIYUN_SECRET_FIELDS,
            crypto=crypto,
            module="aliyun_access",
            schema_version=schema_version,
            now=now,
        )

    result["credential_mode"] = selected_mode
    for field in ("region_id", "openapi_network"):
        value = getattr(mutation, field)
        if value is not None:
            result[field] = value

    if previous_mode is not None and previous_mode != selected_mode:
        if transition.previous_mode_action == "clear":
            result.pop(previous_mode, None)
    for mode in transition.delete_retained_modes:
        result.pop(mode, None)

    return result, AliyunTransitionAudit(
        previous_mode=previous_mode,
        selected_mode=selected_mode,
        previous_mode_action=transition.previous_mode_action,
        selected_mode_action=validation_transition.selected_mode_action,
        reused_direct_source=transition.reuse_direct_ak_as_assume_source,
        credential_id_mask=_stored_credential_id_mask(result, selected_mode),
        delete_retained_modes=tuple(sorted(set(transition.delete_retained_modes))),
    )
