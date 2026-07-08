from __future__ import annotations

import enum
from typing import TYPE_CHECKING

from sqlalchemy import Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from server.models.base import Base, TimestampMixin, generate_uuid

if TYPE_CHECKING:
    from server.models.instance import Instance
    from server.models.user import User


class AccountType(str, enum.Enum):
    NORMAL = "normal"
    SUPER = "super"


class TenantProvisioningStep(str, enum.Enum):
    PENDING = "pending"
    RESOURCE_CONFIG = "resource_config"
    TENANT = "tenant"
    USER = "user"
    DATABASE = "database"
    GRANT = "grant"


class DBAccount(TimestampMixin, Base):
    __tablename__ = "db_accounts"
    __table_args__ = (
        UniqueConstraint("instance_id", "user_id", name="uq_instance_user_account"),
        UniqueConstraint("instance_id", "account_name", name="uq_instance_account_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    instance_id: Mapped[str] = mapped_column(String(36), ForeignKey("instances.id"), index=True)
    user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    account_name: Mapped[str] = mapped_column(String(255))
    account_password_enc: Mapped[str] = mapped_column(String(512))
    account_type: Mapped[AccountType] = mapped_column(Enum(AccountType), default=AccountType.NORMAL)
    tenant_name: Mapped[str | None] = mapped_column(String(10), nullable=True)
    provisioning_step: Mapped[TenantProvisioningStep | None] = mapped_column(
        Enum(TenantProvisioningStep), nullable=True
    )

    instance: Mapped["Instance"] = relationship(back_populates="db_accounts", lazy="selectin")
    user: Mapped["User | None"] = relationship(lazy="selectin")
