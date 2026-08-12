from __future__ import annotations

import importlib
import json

import pytest


def _profile_module():
    return importlib.import_module("server.core.dedicated_purchase_profile")


def test_profile_generates_only_supported_agentic_shape() -> None:
    profile = _profile_module()

    payload = profile.build_purchase_config(storage_type="essdpl1")

    assert payload == {
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
        "storage_type": "essdpl1",
        "storage_space": "20",
    }


def test_profile_rejects_unknown_storage_type() -> None:
    profile = _profile_module()

    with pytest.raises(profile.UnsupportedStorageType):
        profile.build_purchase_config(storage_type="ESSDAUTOPL")


@pytest.mark.parametrize(
    "stored",
    [
        "{}",
        "not-json",
        json.dumps({"db_minor_version": "8.0.1", "storage_type": "essdpl1"}),
    ],
)
def test_legacy_profile_requires_explicit_upgrade(stored: str) -> None:
    profile = _profile_module()

    assert profile.classify_legacy_purchase_config(stored) == (
        profile.PurchaseProfileStatus.UPGRADE_REQUIRED
    )


def test_canonical_snapshot_is_valid_legacy_profile() -> None:
    profile = _profile_module()
    stored = json.dumps(
        profile.build_purchase_config(storage_type="essdpl1"),
        separators=(",", ":"),
        sort_keys=True,
    )

    assert profile.classify_legacy_purchase_config(stored) == (
        profile.PurchaseProfileStatus.VALID
    )
