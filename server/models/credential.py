from __future__ import annotations

import enum
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Enum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from server.models.base import Base, TimestampMixin, generate_uuid

if TYPE_CHECKING:
    from server.models.db_instance_resource import DBInstanceResource
    from server.models.instance import Instance
    from server.models.user import User


class CredentialPurpose(str, enum.Enum):
    PROVISIONING_ADMIN = "provisioning_admin"
    DIRECT_ACCESS = "direct_access"
    RESOURCE_ACCESS = "resource_access"


class CredentialCapability(str, enum.Enum):
    READONLY = "readonly"
    READWRITE = "readwrite"
    ADMIN = "admin"


class CredentialStatus(str, enum.Enum):
    ACTIVE = "active"
    REVOKED = "revoked"


class InstanceCredential(TimestampMixin, Base):
    __tablename__ = "instance_credentials"
    __table_args__ = (
        CheckConstraint(
            "(instance_id IS NOT NULL AND resource_id IS NULL) OR (instance_id IS NULL AND resource_id IS NOT NULL)",
            name="ck_instance_credentials_exactly_one_owner",
        ),
        CheckConstraint(
            "status != 'ACTIVE' OR (username_ciphertext IS NOT NULL AND password_ciphertext IS NOT NULL)",
            name="ck_instance_credentials_active_has_ciphertext",
        ),
        CheckConstraint("version > 0", name="ck_instance_credentials_version_positive"),
        UniqueConstraint("instance_id", "name", name="uq_instance_credentials_instance_name"),
        UniqueConstraint(
            "resource_id",
            "purpose",
            name="uq_instance_credentials_resource_purpose",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    instance_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("instances.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    resource_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey(
            "db_instance_resources.id",
            name="fk_instance_credentials_resource_id",
            ondelete="CASCADE",
            use_alter=True,
        ),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255))
    purpose: Mapped[CredentialPurpose] = mapped_column(Enum(CredentialPurpose, native_enum=False, length=32))
    capability: Mapped[CredentialCapability] = mapped_column(Enum(CredentialCapability, native_enum=False, length=32))
    username_ciphertext: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    password_ciphertext: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    database_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[CredentialStatus] = mapped_column(
        Enum(CredentialStatus, native_enum=False, length=32),
        default=CredentialStatus.ACTIVE,
    )
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_by_user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)

    instance: Mapped["Instance | None"] = relationship(back_populates="credentials", lazy="selectin")
    resource: Mapped["DBInstanceResource | None"] = relationship(back_populates="credentials", lazy="selectin")
    created_by: Mapped["User | None"] = relationship(lazy="selectin")
