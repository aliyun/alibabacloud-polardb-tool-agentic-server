from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from server.models.base import Base, TimestampMixin, generate_uuid


DEFAULT_IDENTITY_SOURCE_STALE_AFTER_SECONDS = 7 * 24 * 60 * 60
_PRINCIPAL_SNAPSHOT_ID_NAMESPACE = uuid.UUID(
    "1c3cd655-5acc-4b3b-9af2-25cbfdef2324"
)


def _principal_snapshot_id(*parts: str) -> str:
    encoded = "".join(f"{len(part)}:{part}" for part in parts)
    return str(uuid.uuid5(_PRINCIPAL_SNAPSHOT_ID_NAMESPACE, encoded))


class IdentitySourceProvider(str, enum.Enum):
    FEISHU = "feishu"
    SHAREPOINT = "sharepoint"


class EnterpriseIdentitySourceStatus(str, enum.Enum):
    PENDING_TENANT_VERIFICATION = "pending_tenant_verification"
    PENDING_BINDING = "pending_binding"
    ACTIVE = "active"
    STALE = "stale"
    DISABLED = "disabled"


class EnterpriseDirectoryEntryStatus(str, enum.Enum):
    ACTIVE = "active"
    DISABLED = "disabled"


class IdentitySourceGroupMembershipType(str, enum.Enum):
    USER = "user"
    GROUP = "group"


class EnterpriseDirectoryPrincipalType(str, enum.Enum):
    GROUP = "group"
    DEPARTMENT = "department"
    ACL_GROUP = "acl_group"


class EnterpriseIdentitySource(TimestampMixin, Base):
    __tablename__ = "enterprise_identity_sources"
    __table_args__ = (
        UniqueConstraint("provider", "tenant_id", name="uq_identity_source_provider_tenant"),
        CheckConstraint(
            "provider IN ('feishu', 'sharepoint')",
            name="ck_identity_source_provider",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    provider: Mapped[IdentitySourceProvider] = mapped_column(
        Enum(
            IdentitySourceProvider,
            native_enum=False,
            length=32,
            values_callable=lambda values: [item.value for item in values],
        )
    )
    tenant_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[EnterpriseIdentitySourceStatus] = mapped_column(
        Enum(
            EnterpriseIdentitySourceStatus,
            native_enum=False,
            length=32,
            values_callable=lambda values: [item.value for item in values],
        ),
        default=EnterpriseIdentitySourceStatus.PENDING_BINDING,
    )
    config_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stale_after_seconds: Mapped[int] = mapped_column(
        Integer,
        default=DEFAULT_IDENTITY_SOURCE_STALE_AFTER_SECONDS,
    )
    last_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    sync_warning_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    sync_worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sync_lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sync_retry_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    sync_next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @classmethod
    def create(
        cls,
        *,
        name: str,
        provider: IdentitySourceProvider,
        tenant_id: str | None,
    ) -> "EnterpriseIdentitySource":
        normalized_name = name.strip()
        normalized_tenant = tenant_id.strip() if tenant_id else None
        if not normalized_name:
            raise ValueError("identity source name is required")
        return cls(
            name=normalized_name,
            provider=provider,
            tenant_id=normalized_tenant,
        )


class EnterpriseIdentitySourceSpaceBinding(TimestampMixin, Base):
    __tablename__ = "enterprise_identity_source_space_bindings"
    __table_args__ = (
        UniqueConstraint("identity_source_id", "knowledge_space_id", name="uq_identity_source_space_binding"),
        Index("ix_identity_source_space_binding_space", "knowledge_space_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    identity_source_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("enterprise_identity_sources.id", ondelete="CASCADE"), index=True
    )
    knowledge_space_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("polarrag_spaces.knowledge_space_id", ondelete="CASCADE")
    )


class FeishuTenantVerificationState(TimestampMixin, Base):
    __tablename__ = "feishu_tenant_verification_states"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    identity_source_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("enterprise_identity_sources.id", ondelete="CASCADE"),
        index=True,
    )
    created_by_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    state_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FeishuUserLoginState(TimestampMixin, Base):
    __tablename__ = "feishu_user_login_states"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    identity_source_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("enterprise_identity_sources.id", ondelete="CASCADE"),
        index=True,
    )
    state_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SharePointUserLoginState(TimestampMixin, Base):
    __tablename__ = "sharepoint_user_login_states"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    identity_source_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("enterprise_identity_sources.id", ondelete="CASCADE"),
        index=True,
    )
    state_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    nonce_ciphertext: Mapped[str] = mapped_column(Text)
    code_verifier_ciphertext: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EnterpriseDirectoryUser(TimestampMixin, Base):
    __tablename__ = "enterprise_directory_users"
    __table_args__ = (
        UniqueConstraint("identity_source_id", "external_user_id", name="uq_directory_user_source_external"),
        Index("ix_directory_user_source_status", "identity_source_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    identity_source_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("enterprise_identity_sources.id", ondelete="CASCADE"), index=True
    )
    external_user_id: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[EnterpriseDirectoryEntryStatus] = mapped_column(
        Enum(
            EnterpriseDirectoryEntryStatus,
            native_enum=False,
            length=16,
            values_callable=lambda values: [item.value for item in values],
        ),
        default=EnterpriseDirectoryEntryStatus.ACTIVE,
    )


class EnterpriseDirectoryGroup(TimestampMixin, Base):
    __tablename__ = "enterprise_directory_groups"
    __table_args__ = (
        UniqueConstraint("identity_source_id", "external_group_id", name="uq_directory_group_source_external"),
        Index("ix_directory_group_source_status", "identity_source_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    identity_source_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("enterprise_identity_sources.id", ondelete="CASCADE"), index=True
    )
    external_group_id: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(255))
    principal_type: Mapped[EnterpriseDirectoryPrincipalType] = mapped_column(
        Enum(
            EnterpriseDirectoryPrincipalType,
            native_enum=False,
            length=16,
            values_callable=lambda values: [item.value for item in values],
        ),
        default=EnterpriseDirectoryPrincipalType.GROUP,
    )
    status: Mapped[EnterpriseDirectoryEntryStatus] = mapped_column(
        Enum(
            EnterpriseDirectoryEntryStatus,
            native_enum=False,
            length=16,
            values_callable=lambda values: [item.value for item in values],
        ),
        default=EnterpriseDirectoryEntryStatus.ACTIVE,
    )


class IdentitySourceGroupMembership(TimestampMixin, Base):
    """Connector-owned mirror used to expand provider group membership."""

    # Keep the published physical table name for rolling-upgrade compatibility.
    __tablename__ = "enterprise_directory_memberships"
    __table_args__ = (
        UniqueConstraint(
            "identity_source_id",
            "external_group_id",
            "member_type",
            "external_member_id",
            name="uq_directory_membership",
        ),
        Index(
            "ix_directory_membership_member",
            "identity_source_id",
            "member_type",
            "external_member_id",
        ),
        Index(
            "ix_enterprise_directory_memberships_identity_source_id",
            "identity_source_id",
        ),
        Index(
            "ix_identity_source_group_memberships_prune",
            "identity_source_id",
            "updated_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    identity_source_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("enterprise_identity_sources.id", ondelete="CASCADE")
    )
    external_group_id: Mapped[str] = mapped_column(String(255))
    member_type: Mapped[IdentitySourceGroupMembershipType] = mapped_column(
        Enum(
            IdentitySourceGroupMembershipType,
            native_enum=False,
            length=16,
            values_callable=lambda values: [item.value for item in values],
        )
    )
    external_member_id: Mapped[str] = mapped_column(String(255))


class IdentitySourceUserPrincipalSnapshotEntry(TimestampMixin, Base):
    """Externally pushed per-user full principal snapshot entry."""

    __tablename__ = "identity_source_user_principal_snapshot_entries"
    __table_args__ = (
        Index(
            "ix_identity_source_user_principal_snapshot_key",
            "identity_source_id",
            "external_user_id",
            "principal_type",
            "principal_id",
        ),
        Index(
            "ix_identity_source_user_principal_snapshot_principal",
            "identity_source_id",
            "principal_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    identity_source_id: Mapped[str] = mapped_column(
        String(36),
    )
    external_user_id: Mapped[str] = mapped_column(String(255))
    principal_type: Mapped[EnterpriseDirectoryPrincipalType] = mapped_column(
        Enum(
            EnterpriseDirectoryPrincipalType,
            native_enum=False,
            length=16,
            values_callable=lambda values: [item.value for item in values],
        )
    )
    principal_id: Mapped[str] = mapped_column(String(255))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @classmethod
    def create(
        cls,
        *,
        identity_source_id: str,
        external_user_id: str,
        principal_type: EnterpriseDirectoryPrincipalType,
        principal_id: str,
        expires_at: datetime | None,
    ) -> IdentitySourceUserPrincipalSnapshotEntry:
        return cls(
            id=_principal_snapshot_id(
                "principal-snapshot",
                identity_source_id,
                external_user_id,
                principal_type.value,
                principal_id,
            ),
            identity_source_id=identity_source_id,
            external_user_id=external_user_id,
            principal_type=principal_type,
            principal_id=principal_id,
            expires_at=expires_at,
        )


# Compatibility aliases keep existing call sites stable while the persisted
# names clearly distinguish connector mirrors from externally pushed snapshots.
EnterpriseDirectoryMembershipType = IdentitySourceGroupMembershipType
EnterpriseDirectoryMembership = IdentitySourceGroupMembership
ExternalUserPrincipalMembership = IdentitySourceUserPrincipalSnapshotEntry
