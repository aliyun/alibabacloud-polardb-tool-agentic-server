from __future__ import annotations

import enum
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from server.models.base import Base, TimestampMixin, generate_uuid

if TYPE_CHECKING:
    from server.models.db_account import DBAccount
    from server.models.department import Department
    from server.models.instance import Instance
    from server.models.user import User


class Permission(str, enum.Enum):
    READONLY = "readonly"
    READWRITE = "readwrite"


class UserDepartment(TimestampMixin, Base):
    __tablename__ = "user_departments"
    __table_args__ = (
        UniqueConstraint("user_id", "department_id", name="uq_user_department"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    department_id: Mapped[str] = mapped_column(String(36), ForeignKey("departments.id"), index=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped["User"] = relationship(back_populates="department_memberships", lazy="selectin")
    department: Mapped["Department"] = relationship(back_populates="user_memberships", lazy="selectin")


class UserInstanceBinding(TimestampMixin, Base):
    __tablename__ = "user_instance_bindings"
    __table_args__ = (
        UniqueConstraint("user_id", "instance_id", name="uq_user_instance"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    instance_id: Mapped[str] = mapped_column(String(36), ForeignKey("instances.id"), index=True)
    db_account_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("db_accounts.id"), nullable=True)
    permission: Mapped[Permission] = mapped_column(Enum(Permission), default=Permission.READWRITE)

    user: Mapped["User"] = relationship(back_populates="instance_bindings", lazy="selectin")
    instance: Mapped["Instance"] = relationship(back_populates="user_bindings", lazy="selectin")
    db_account: Mapped["DBAccount | None"] = relationship(lazy="selectin")


class DepartmentInstanceBinding(TimestampMixin, Base):
    __tablename__ = "department_instance_bindings"
    __table_args__ = (
        UniqueConstraint("department_id", "instance_id", name="uq_dept_instance"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    department_id: Mapped[str] = mapped_column(String(36), ForeignKey("departments.id"), index=True)
    instance_id: Mapped[str] = mapped_column(String(36), ForeignKey("instances.id"), index=True)
    tenant_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    default_permission: Mapped[Permission] = mapped_column(Enum(Permission), default=Permission.READWRITE)

    department: Mapped["Department"] = relationship(back_populates="instance_bindings", lazy="selectin")
    instance: Mapped["Instance"] = relationship(back_populates="department_bindings", lazy="selectin")
