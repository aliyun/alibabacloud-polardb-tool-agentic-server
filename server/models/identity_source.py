from __future__ import annotations

import enum
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


class EnterpriseDirectoryMembershipType(str, enum.Enum):
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
    stale_after_seconds: Mapped[int] = mapped_column(Integer, default=1800)
    last_error: Mapped[str | None] = mapped_column(String(512), nullable=True)

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


class EnterpriseDirectoryMembership(TimestampMixin, Base):
    __tablename__ = "enterprise_directory_memberships"
    __table_args__ = (
        UniqueConstraint(
            "identity_source_id",
            "external_group_id",
            "member_type",
            "external_member_id",
            name="uq_directory_membership",
        ),
        Index("ix_directory_membership_member", "identity_source_id", "member_type", "external_member_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    identity_source_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("enterprise_identity_sources.id", ondelete="CASCADE"), index=True
    )
    external_group_id: Mapped[str] = mapped_column(String(255))
    member_type: Mapped[EnterpriseDirectoryMembershipType] = mapped_column(
        Enum(
            EnterpriseDirectoryMembershipType,
            native_enum=False,
            length=16,
            values_callable=lambda values: [item.value for item in values],
        )
    )
    external_member_id: Mapped[str] = mapped_column(String(255))
