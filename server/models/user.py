from __future__ import annotations

import enum
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import Enum, ForeignKey, Integer, String
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


class PasswordState(StrEnum):
    RESET_REQUIRED = "RESET_REQUIRED"
    ACTIVE = "ACTIVE"


class User(TimestampMixin, Base):
    __tablename__ = "users"

    def __init__(self, **kwargs: object) -> None:
        kwargs.setdefault("credential_epoch", 1)
        super().__init__(**kwargs)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    external_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    auth_provider: Mapped[AuthProvider] = mapped_column(
        Enum(AuthProvider, native_enum=False),
        default=AuthProvider.BUILTIN,
    )
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password_state: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    credential_epoch: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
    )
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, native_enum=False),
        default=UserRole.MEMBER,
    )
    status: Mapped[UserStatus] = mapped_column(
        Enum(UserStatus, native_enum=False),
        default=UserStatus.ACTIVE,
    )
    default_instance_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey(
            "instances.id",
            name="fk_users_default_instance_id",
            ondelete="SET NULL",
            use_alter=True,
        ),
        nullable=True,
    )
    provisioning_mode: Mapped[ProvisioningMode | None] = mapped_column(
        Enum(ProvisioningMode, native_enum=False),
        nullable=True,
    )

    @property
    def effective_password_state(self) -> PasswordState:
        """Return the application state, preserving legacy user behavior."""
        if self.password_state is None:
            return PasswordState.ACTIVE
        return PasswordState(self.password_state)

    department_memberships: Mapped[list["UserDepartment"]] = relationship(back_populates="user", lazy="selectin")
    instance_bindings: Mapped[list["UserInstanceBinding"]] = relationship(back_populates="user", lazy="selectin")
