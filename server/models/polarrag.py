from __future__ import annotations

import enum
import hashlib
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from server.models.base import Base, TimestampMixin, generate_uuid


EXTERNAL_ENTERPRISE_PRINCIPAL_PROVIDERS = frozenset(
    {"feishu", "sharepoint"}
)
ACL_CONTEXT_PRINCIPAL_PROVIDERS = (
    EXTERNAL_ENTERPRISE_PRINCIPAL_PROVIDERS | {"polarrag"}
)


def _enum_values(enum_type: type[enum.Enum]) -> list[str]:
    return [str(member.value) for member in enum_type]


def _enum_column(enum_type: type[enum.Enum], *, length: int) -> Enum:
    return Enum(
        enum_type,
        native_enum=False,
        length=length,
        validate_strings=True,
        values_callable=_enum_values,
    )


class PolarRAGInstanceStatus(str, enum.Enum):
    PENDING = "pending"
    ACTIVE = "active"
    ERROR = "error"
    CAPABILITY_MISSING = "capability_missing"
    DISABLED = "disabled"


class EnterprisePrincipalType(str, enum.Enum):
    USER = "user"
    GROUP = "group"


class EnterprisePrincipalSource(str, enum.Enum):
    ADMIN_MANAGED = "admin_managed"
    REMOTE_RESOLVER = "remote_resolver"


class EnterprisePrincipalStatus(str, enum.Enum):
    ACTIVE = "active"
    DISABLED = "disabled"


class KnowledgeBindingMode(str, enum.Enum):
    DOMAIN = "domain"
    OWNER = "owner"


class KnowledgeResourceSyncStatus(str, enum.Enum):
    ACTIVE = "active"
    UPSTREAM_DISABLED = "upstream_disabled"
    OWNER_UNRESOLVED = "owner_unresolved"
    UNSUPPORTED_KB_TYPE = "unsupported_kb_type"


class PolarRAGInstance(TimestampMixin, Base):
    __tablename__ = "polarrag_instances"
    __table_args__ = (
        CheckConstraint("scheme IN ('http', 'https')", name="ck_polarrag_instances_scheme"),
        CheckConstraint("port >= 1 AND port <= 65535", name="ck_polarrag_instances_port"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    scheme: Mapped[str] = mapped_column(String(8))
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer)
    username_ciphertext: Mapped[str] = mapped_column(Text)
    password_ciphertext: Mapped[str] = mapped_column(Text)
    tls_verify: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    ca_bundle_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[PolarRAGInstanceStatus] = mapped_column(
        _enum_column(PolarRAGInstanceStatus, length=32),
        default=PolarRAGInstanceStatus.PENDING,
    )
    plugin_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    capabilities_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="RESTRICT"))

class PolarRAGSpace(TimestampMixin, Base):
    __tablename__ = "polarrag_spaces"
    __table_args__ = (
        UniqueConstraint(
            "polarrag_instance_id",
            "space_id",
            name="uq_polarrag_spaces_instance_space",
        ),
    )

    knowledge_space_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=generate_uuid,
    )
    polarrag_instance_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("polarrag_instances.id", ondelete="CASCADE"),
        index=True,
    )
    space_id: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(255))
    identity_domain: Mapped[str] = mapped_column(String(255))
    oss_bucket: Mapped[str | None] = mapped_column(String(255), nullable=True)
    oss_endpoint: Mapped[str | None] = mapped_column(String(512), nullable=True)
    oss_access_key_id_ciphertext: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    oss_access_key_secret_ciphertext: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    oss_object_prefix: Mapped[str | None] = mapped_column(String(512), nullable=True)
    oss_config_validated: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=false(),
    )
    oss_validated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    oss_last_error_code: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    instance: Mapped[PolarRAGInstance] = relationship(lazy="selectin")


class KnowledgeResource(TimestampMixin, Base):
    __tablename__ = "knowledge_resources"
    __table_args__ = (
        UniqueConstraint(
            "polarrag_instance_id",
            "space_id",
            "kb_id",
            name="uq_knowledge_resources_upstream",
        ),
        Index(
            "ix_knowledge_resources_discovery",
            "enabled",
            "sync_status",
            "identity_domain",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    knowledge_space_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("polarrag_spaces.knowledge_space_id", ondelete="CASCADE"),
        index=True,
    )
    polarrag_instance_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("polarrag_instances.id", ondelete="CASCADE"),
        index=True,
    )
    space_id: Mapped[str] = mapped_column(String(255))
    kb_id: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(255))
    usage: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    kb_type: Mapped[str] = mapped_column(String(32))
    identity_domain: Mapped[str] = mapped_column(String(255), index=True)
    binding_mode: Mapped[KnowledgeBindingMode | None] = mapped_column(
        _enum_column(KnowledgeBindingMode, length=16),
        nullable=True,
    )
    owner_pas_user_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    sync_status: Mapped[KnowledgeResourceSyncStatus] = mapped_column(
        _enum_column(KnowledgeResourceSyncStatus, length=32),
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    upstream_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    space: Mapped[PolarRAGSpace] = relationship(lazy="selectin")


class EnterprisePrincipalAssignment(TimestampMixin, Base):
    __tablename__ = "enterprise_principal_assignments"
    __table_args__ = (
        UniqueConstraint(
            "pas_user_id",
            "identity_domain",
            "provider",
            "principal_type",
            "principal_id",
            name="uq_enterprise_principal_assignment",
        ),
        UniqueConstraint(
            "user_principal_key",
            name="uq_enterprise_user_principal_key",
        ),
        CheckConstraint(
            "provider IN ('feishu', 'sharepoint', 'polarrag')",
            name="ck_enterprise_principal_provider",
        ),
        CheckConstraint(
            "provider != 'polarrag' OR principal_type = 'user'",
            name="ck_enterprise_principal_native_user",
        ),
        CheckConstraint(
            "provider != 'polarrag' OR source = 'admin_managed'",
            name="ck_enterprise_principal_native_admin",
        ),
        Index(
            "ix_enterprise_principal_resolution",
            "pas_user_id",
            "identity_domain",
            "status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    pas_user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    identity_domain: Mapped[str] = mapped_column(String(255))
    provider: Mapped[str] = mapped_column(String(32))
    principal_type: Mapped[EnterprisePrincipalType] = mapped_column(
        _enum_column(EnterprisePrincipalType, length=16)
    )
    principal_id: Mapped[str] = mapped_column(String(255))
    source: Mapped[EnterprisePrincipalSource] = mapped_column(
        _enum_column(EnterprisePrincipalSource, length=32)
    )
    status: Mapped[EnterprisePrincipalStatus] = mapped_column(
        _enum_column(EnterprisePrincipalStatus, length=16),
        default=EnterprisePrincipalStatus.ACTIVE,
    )
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    user_principal_key: Mapped[str | None] = mapped_column(String(64), nullable=True)

    @classmethod
    def create(
        cls,
        *,
        pas_user_id: str,
        identity_domain: str,
        provider: str,
        principal_type: EnterprisePrincipalType,
        principal_id: str,
        source: EnterprisePrincipalSource,
        status: EnterprisePrincipalStatus = EnterprisePrincipalStatus.ACTIVE,
        valid_until: datetime | None = None,
        canonical_user_external_id: str | None = None,
    ) -> "EnterprisePrincipalAssignment":
        normalized_provider = provider.strip().lower()
        normalized_domain = identity_domain.strip()
        if normalized_provider not in ACL_CONTEXT_PRINCIPAL_PROVIDERS:
            raise ValueError("provider is not allowed")
        if normalized_provider == "polarrag":
            if principal_type != EnterprisePrincipalType.USER:
                raise ValueError(
                    "polarrag provider only supports user principals"
                )
            if source != EnterprisePrincipalSource.ADMIN_MANAGED:
                raise ValueError("polarrag principals must be admin-managed")
            if principal_id != (canonical_user_external_id or ""):
                raise ValueError("polarrag principal must match the PAS user")
            normalized_id = canonical_user_external_id or ""
        else:
            normalized_id = principal_id.strip()
        if not normalized_domain:
            raise ValueError("identity_domain is required")
        if not normalized_id:
            raise ValueError("principal_id is required")
        user_principal_key = None
        if principal_type == EnterprisePrincipalType.USER:
            material = "\x00".join(
                (normalized_domain, normalized_provider, principal_type.value, normalized_id)
            )
            user_principal_key = hashlib.sha256(material.encode("utf-8")).hexdigest()
        return cls(
            pas_user_id=pas_user_id,
            identity_domain=normalized_domain,
            provider=normalized_provider,
            principal_type=principal_type,
            principal_id=normalized_id,
            source=source,
            status=status,
            valid_until=valid_until,
            user_principal_key=user_principal_key,
        )
