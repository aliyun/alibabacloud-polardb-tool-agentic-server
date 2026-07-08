from __future__ import annotations

import enum
from typing import TYPE_CHECKING

from sqlalchemy import Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from server.models.base import Base, TimestampMixin, generate_uuid

if TYPE_CHECKING:
    from server.models.binding import UserDepartment, UserInstanceBinding


class AuthProvider(str, enum.Enum):
    OIDC = "oidc"
    BUILTIN = "builtin"


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    MEMBER = "member"


class UserStatus(str, enum.Enum):
    ACTIVE = "active"
    DISABLED = "disabled"


class ProvisioningMode(str, enum.Enum):
    DEDICATED = "dedicated"
    MULTITENANT = "multitenant"


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    external_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    auth_provider: Mapped[AuthProvider] = mapped_column(Enum(AuthProvider), default=AuthProvider.BUILTIN)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.MEMBER)
    status: Mapped[UserStatus] = mapped_column(Enum(UserStatus), default=UserStatus.ACTIVE)
    default_instance_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("instances.id", ondelete="SET NULL"), nullable=True
    )
    provisioning_mode: Mapped[ProvisioningMode | None] = mapped_column(Enum(ProvisioningMode), nullable=True)

    department_memberships: Mapped[list["UserDepartment"]] = relationship(back_populates="user", lazy="selectin")
    instance_bindings: Mapped[list["UserInstanceBinding"]] = relationship(back_populates="user", lazy="selectin")
