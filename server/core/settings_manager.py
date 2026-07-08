from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.core.crypto import encrypt, decrypt
from server.models.system_setting import SystemSetting, SETTINGS_SCHEMA

logger = logging.getLogger(__name__)


def _is_masked_value(value: str) -> bool:
    """Return True if value looks like a masked secret (e.g. 'ABCD****WXYZ')."""
    return bool(re.match(r"^[^\*]{0,4}\*{4,}[^\*]{0,4}$", value))


def _mask_secret(plaintext: str) -> str:
    """Return masked form: first 4 + **** + last 4 chars; **** if < 8 chars."""
    if len(plaintext) < 8:
        return "****"
    return plaintext[:4] + "****" + plaintext[-4:]


async def get_setting(
    session: AsyncSession, key: str, default: str | None = None
) -> str | None:
    row = await session.execute(
        select(SystemSetting).where(SystemSetting.key == key)
    )
    setting = row.scalar_one_or_none()
    if setting is None:
        schema = SETTINGS_SCHEMA.get(key)
        return schema.default if schema else default
    return setting.value


async def get_setting_raw(
    session: AsyncSession, key: str, default: str | None = None
) -> str | None:
    """Get a setting value, decrypting secret fields for internal backend use."""
    row = await session.execute(
        select(SystemSetting).where(SystemSetting.key == key)
    )
    setting = row.scalar_one_or_none()
    if setting is None:
        schema = SETTINGS_SCHEMA.get(key)
        return schema.default if schema else default
    schema = SETTINGS_SCHEMA.get(key)
    if schema and schema.type == "secret" and setting.value:
        return decrypt(setting.value)
    return setting.value


async def set_setting(session: AsyncSession, key: str, value: str) -> None:
    if key not in SETTINGS_SCHEMA:
        raise ValueError(f"Unknown setting key: {key}")
    _validate_type(key, value)

    schema = SETTINGS_SCHEMA[key]
    store_value = value
    if schema.type == "secret" and value:
        store_value = encrypt(value)

    row = await session.execute(
        select(SystemSetting).where(SystemSetting.key == key)
    )
    setting = row.scalar_one_or_none()
    if setting is None:
        setting = SystemSetting(key=key, value=store_value, description=schema.description)
        session.add(setting)
    else:
        setting.value = store_value
    await session.commit()


async def batch_update_settings(
    session: AsyncSession, updates: dict[str, str]
) -> None:
    errors: list[str] = []
    # Filter out masked secret values before validation
    filtered_updates: dict[str, str] = {}
    for key, value in updates.items():
        if key not in SETTINGS_SCHEMA:
            errors.append(f"Unknown setting key: {key}")
            continue
        schema = SETTINGS_SCHEMA[key]
        if schema.type == "secret" and _is_masked_value(value):
            continue  # skip masked values — user didn't change this secret
        filtered_updates[key] = value

    for key, value in filtered_updates.items():
        try:
            _validate_type(key, value)
        except ValueError as e:
            errors.append(str(e))
    if errors:
        raise ValueError("; ".join(errors))

    for key, value in filtered_updates.items():
        schema = SETTINGS_SCHEMA[key]
        store_value = value
        if schema.type == "secret" and value:
            store_value = encrypt(value)

        row = await session.execute(
            select(SystemSetting).where(SystemSetting.key == key)
        )
        setting = row.scalar_one_or_none()
        if setting is None:
            session.add(SystemSetting(key=key, value=store_value, description=schema.description))
        else:
            setting.value = store_value
    await session.commit()


async def get_all_settings(session: AsyncSession) -> list[dict[str, Any]]:
    rows = await session.execute(select(SystemSetting))
    settings = {s.key: s for s in rows.scalars().all()}
    result = []
    for key, schema in SETTINGS_SCHEMA.items():
        s = settings.get(key)
        raw_value = s.value if s else schema.default
        display_value = raw_value
        if schema.type == "secret" and s and s.value:
            try:
                plaintext = decrypt(s.value)
                display_value = _mask_secret(plaintext)
            except Exception:
                display_value = "****"
        result.append({
            "key": key,
            "value": display_value,
            "description": schema.description,
            "type": schema.type,
            "required": schema.required,
        })
    return result


def _validate_type(key: str, value: str) -> None:
    schema = SETTINGS_SCHEMA[key]
    if schema.type == "int":
        if value == "" and not schema.required:
            return
        try:
            int(value)
        except ValueError:
            raise ValueError(f"Setting '{key}' must be an integer, got '{value}'")
    elif schema.type == "bool":
        if value not in ("true", "false"):
            raise ValueError(f"Setting '{key}' must be 'true' or 'false', got '{value}'")

    # Custom validation for credential mode
    if key == "aliyun_credential_mode" and value not in ("direct_ak", "assume_role"):
        raise ValueError(
            f"Setting 'aliyun_credential_mode' must be 'direct_ak' or 'assume_role', got '{value}'"
        )
