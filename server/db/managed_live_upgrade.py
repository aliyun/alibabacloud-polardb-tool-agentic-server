"""Release-specific admission for managed MySQL 8.0.13 legacy live peers.

This exception does not publish compatibility with old images or reclassify the
CONTRACT release. Unknown sources and engines keep explicit maintenance admission.
"""
from __future__ import annotations

from collections.abc import Mapping
import re

from server.db.schema_contract import digest

TARGET_DIGEST = "2c71393034dafa0c337f7d0b12b39ccdc5eaa36e140a3149627c03b6bc268e8f"
SOURCES = {
    "9c1d2e3f4a5b": (1, "6a1003bea44cfbbf1ed7c4d39a013c7505659574137d6fe0cb20b24f03857120"),
    "cf4a5b6c7d8e": (3, "8dee264f848ec7b08329382b72ea0b02f675c657dd5e83c9520fd419c0b81286"),
    "2c3d4e5f6a7b": (7, "65f7f798736beb2ba768703e3eb6781794c40c555ed49e56e8d83d0d28633d9a"),
}


def allows_managed_live_upgrade(*, manifest: dict, heads: tuple[str, ...], published: dict | None,
        dialect: str, server_version: str, env: Mapping[str, str], source_pristine: bool) -> bool:
    if (digest(manifest) != TARGET_DIGEST or dialect != "mysql" or server_version != "8.0.13"
            or not source_pristine or len(heads) != 1 or heads[0] not in SOURCES
            or not re.fullmatch(r"pa-[A-Za-z0-9]+", env.get("PAS_MANAGED_INSTANCE_ID", ""))
            or not re.fullmatch(r"[1-9][0-9]{0,8}", env.get("PAS_MANAGED_INSTANCE_GENERATION", ""))
            or not isinstance(published, dict)):
        return False
    sequence, source_digest = SOURCES[heads[0]]
    return (all(type(published.get(k)) is int for k in ("protocol_version", "generation", "completed_sequence"))
        and published.get("protocol_version") == 1 and published.get("generation") == 1
        and published.get("completed_head") == heads[0] and published.get("completed_sequence") == sequence
        and isinstance(published.get("compatible_manifests"), list)
        and source_digest in published["compatible_manifests"])


def pristine_source(connection) -> bool:
    import sqlalchemy as sa

    inspector = sa.inspect(connection)
    tables = set(inspector.get_table_names())
    if "enterprise_directory_memberships" not in tables or "identity_source_group_memberships" in tables:
        return False  # Prerelease repair/rename cannot occur with an old reader alive.
    if tables & {"dingtalk_login_states", "external_knowledge_scope_snapshots", "external_knowledge_scope_grants"}:
        return False
    for table, additions in (("agents", {"external_knowledge_auto_bind_enabled"}),
                             ("enterprise_identity_sources", {"permission_observed_at", "permission_error"})):
        if table not in tables or additions & {c["name"] for c in inspector.get_columns(table)}:
            return False
    return True
