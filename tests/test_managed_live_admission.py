import json
from pathlib import Path

import pytest

from server.db.managed_live_upgrade import allows_managed_live_upgrade


def inputs():
    manifest = json.loads((Path(__file__).parents[1] / "server/db/schema_manifest.json").read_text())
    return dict(manifest=manifest, heads=("2c3d4e5f6a7b",), published={"generation": 1,
        "protocol_version": 1, "completed_head": "2c3d4e5f6a7b", "completed_sequence": 7,
        "compatible_manifests": ["65f7f798736beb2ba768703e3eb6781794c40c555ed49e56e8d83d0d28633d9a"]},
        dialect="mysql", server_version="8.0.13", env={"PAS_MANAGED_INSTANCE_ID": "pa-local108test",
        "PAS_MANAGED_INSTANCE_GENERATION": "1"}, source_pristine=True)


def test_verified_managed_mysql_source_admitted():
    assert allows_managed_live_upgrade(**inputs())


@pytest.mark.parametrize("key,value", [("dialect", "postgresql"), ("server_version", "8.0.46"),
    ("env", {}), ("source_pristine", False), ("heads", ("unknown",)), ("published", None)])
def test_other_engines_unmanaged_unknown_or_drifted_sources_keep_guard(key, value):
    data = inputs()
    data[key] = value
    assert not allows_managed_live_upgrade(**data)


@pytest.mark.parametrize("change", ["manifest", "generation", "head", "digest", "instance_generation"])
def test_contract_and_identity_must_match_exactly(change):
    data = inputs()
    if change == "manifest":
        data["manifest"]["required_sequence"] = 2
    elif change == "generation":
        data["published"]["generation"] = 99
    elif change == "head":
        data["published"]["completed_head"] = "different"
    elif change == "digest":
        data["published"]["compatible_manifests"] = ["unknown"]
    else:
        data["env"]["PAS_MANAGED_INSTANCE_GENERATION"] = "0"
    assert not allows_managed_live_upgrade(**data)


def test_affected_physical_objects_must_be_pristine():
    import sqlalchemy as sa
    from server.db.managed_live_upgrade import pristine_source

    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            for name in ("agents", "enterprise_identity_sources", "enterprise_directory_memberships"):
                connection.exec_driver_sql(f"CREATE TABLE {name} (id VARCHAR(36) PRIMARY KEY)")
            assert pristine_source(connection)
            connection.exec_driver_sql("ALTER TABLE agents ADD COLUMN external_knowledge_auto_bind_enabled BOOLEAN")
            assert not pristine_source(connection)
    finally:
        engine.dispose()
