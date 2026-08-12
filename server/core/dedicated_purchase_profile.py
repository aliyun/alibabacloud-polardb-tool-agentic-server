from __future__ import annotations

import enum
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

PROFILE_ID = "agentic-dedicated-mysql"
PROFILE_REVISION = 1
DEFAULT_STORAGE_TYPE = "essdpl1"
SUPPORTED_STORAGE_TYPES = (DEFAULT_STORAGE_TYPE,)

FIXED_PARAMETERS: Mapping[str, str] = MappingProxyType(
    {
        "db_type": "MySQL",
        "db_version": "8.0",
        "db_minor_version": "8.0.2",
        "db_node_class": "polar.mysql.sl.small.c",
        "pay_type": "Postpaid",
        "serverless_type": "AgileServerless",
        "proxy_type": "GENERAL",
        "proxy_class": "polar.maxscale.g2.medium.c",
        "scale_min": "0",
        "scale_max": "4",
        "allow_shut_down": "true",
        "agentic_db_type": "dedicated",
        "scale_ro_num_min": "0",
        "scale_ro_num_max": "1",
        "storage_space": "20",
    }
)


@dataclass(frozen=True, slots=True)
class DedicatedPurchaseProfile:
    profile_id: str
    revision: int
    default_storage_type: str
    supported_storage_types: tuple[str, ...]
    fixed_parameters: Mapping[str, str]


AGENTIC_DEDICATED_PROFILE = DedicatedPurchaseProfile(
    profile_id=PROFILE_ID,
    revision=PROFILE_REVISION,
    default_storage_type=DEFAULT_STORAGE_TYPE,
    supported_storage_types=SUPPORTED_STORAGE_TYPES,
    fixed_parameters=FIXED_PARAMETERS,
)


class PurchaseProfileStatus(str, enum.Enum):
    VALID = "valid"
    UPGRADE_REQUIRED = "upgrade_required"


class UnsupportedStorageType(ValueError):
    pass


def build_purchase_config(*, storage_type: str) -> dict[str, str]:
    if storage_type not in SUPPORTED_STORAGE_TYPES:
        raise UnsupportedStorageType(
            f"Unsupported Dedicated storage type: {storage_type}"
        )
    return {**FIXED_PARAMETERS, "storage_type": storage_type}


def classify_legacy_purchase_config(value: str) -> PurchaseProfileStatus:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return PurchaseProfileStatus.UPGRADE_REQUIRED
    if not isinstance(parsed, dict):
        return PurchaseProfileStatus.UPGRADE_REQUIRED
    storage_type = parsed.get("storage_type")
    if not isinstance(storage_type, str):
        return PurchaseProfileStatus.UPGRADE_REQUIRED
    try:
        expected = build_purchase_config(storage_type=storage_type)
    except UnsupportedStorageType:
        return PurchaseProfileStatus.UPGRADE_REQUIRED
    return (
        PurchaseProfileStatus.VALID
        if parsed == expected
        else PurchaseProfileStatus.UPGRADE_REQUIRED
    )
