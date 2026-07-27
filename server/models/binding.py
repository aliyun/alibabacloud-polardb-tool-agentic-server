from __future__ import annotations

import enum
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    String,
    UniqueConstraint,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from server.models.base import Base, TimestampMixin, generate_uuid

if TYPE_CHECKING:
    from server.models.agent import Agent
    from server.models.credential import InstanceCredential
    from server.models.department import Department
    from server.models.instance import Instance
    from server.models.provisioning_backend import ProvisioningBackend
    from server.models.user import User


class Permission(str, enum.Enum):
    READONLY = "readonly"
    READWRITE = "readwrite"


class BindingOrigin(str, enum.Enum):
    SYSTEM = "system"
    ADMIN = "admin"


class BindingCapability(str, enum.Enum):
    DB_INSTANCE_LIST = "db_instance:list"
    DB_INSTANCE_DESCRIBE = "db_instance:describe"
    DB_INSTANCE_CREDENTIALS_READ = "db_instance:credentials:read"
    SQL_READ = "sql:read"
    SQL_WRITE = "sql:write"


def _binding_capability_values(enum_type: type[enum.Enum]) -> list[str]:
    return [str(member.value) for member in enum_type]


def _binding_capability_type() -> Enum:
    return Enum(
        BindingCapability,
        native_enum=False,
        length=64,
        validate_strings=True,
        values_callable=_binding_capability_values,
    )


class TenantProvisioningStep(str, enum.Enum):
    PENDING = "pending"
    RESOURCE_CONFIG = "resource_config"
    TENANT = "tenant"
    USER = "user"
    DATABASE = "database"
    GRANT = "grant"


class UserDepartment(TimestampMixin, Base):
    __tablename__ = "user_departments"
    __table_args__ = (UniqueConstraint("user_id", "department_id", name="uq_user_department"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    department_id: Mapped[str] = mapped_column(String(36), ForeignKey("departments.id"), index=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped["User"] = relationship(back_populates="department_memberships", lazy="selectin")
    department: Mapped["Department"] = relationship(back_populates="user_memberships", lazy="selectin")


class UserInstanceBinding(TimestampMixin, Base):
    __tablename__ = "user_instance_bindings"
    __table_args__ = (UniqueConstraint("user_id", "instance_id", name="uq_user_instance"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    instance_id: Mapped[str] = mapped_column(String(36), ForeignKey("instances.id"), index=True)
    credential_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("instance_credentials.id", ondelete="RESTRICT"),
        nullable=True,
    )
    permission: Mapped[Permission] = mapped_column(
        Enum(Permission, native_enum=False, length=32),
        default=Permission.READWRITE,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    origin: Mapped[BindingOrigin] = mapped_column(
        Enum(BindingOrigin, native_enum=False, length=32),
        default=BindingOrigin.ADMIN,
    )
    tenant_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provisioning_step: Mapped[TenantProvisioningStep | None] = mapped_column(
        Enum(TenantProvisioningStep, native_enum=False, length=64),
        nullable=True,
    )

    user: Mapped["User"] = relationship(back_populates="instance_bindings", lazy="selectin")
    instance: Mapped["Instance"] = relationship(back_populates="user_bindings", lazy="selectin")
    credential: Mapped["InstanceCredential | None"] = relationship(lazy="selectin")
    capabilities: Mapped[list["UserInstanceBindingCapability"]] = relationship(
        back_populates="binding",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class DepartmentInstanceBinding(TimestampMixin, Base):
    __tablename__ = "department_instance_bindings"
    __table_args__ = (UniqueConstraint("department_id", "instance_id", name="uq_dept_instance"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    department_id: Mapped[str] = mapped_column(String(36), ForeignKey("departments.id"), index=True)
    instance_id: Mapped[str] = mapped_column(String(36), ForeignKey("instances.id"), index=True)
    tenant_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    default_permission: Mapped[Permission] = mapped_column(
        Enum(Permission, native_enum=False, length=32),
        default=Permission.READWRITE,
    )

    department: Mapped["Department"] = relationship(back_populates="instance_bindings", lazy="selectin")
    instance: Mapped["Instance"] = relationship(back_populates="department_bindings", lazy="selectin")


class UserInstanceBindingCapability(Base):
    __tablename__ = "user_instance_binding_capabilities"
    __table_args__ = (
        CheckConstraint(
            "capability IN ('db_instance:list', 'db_instance:describe', "
            "'db_instance:credentials:read', 'sql:read', 'sql:write')",
            name="ck_user_instance_binding_capabilities_value",
        ),
    )

    binding_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("user_instance_bindings.id", ondelete="CASCADE"),
        primary_key=True,
    )
    capability: Mapped[BindingCapability] = mapped_column(
        _binding_capability_type(),
        primary_key=True,
    )

    binding: Mapped["UserInstanceBinding"] = relationship(back_populates="capabilities", lazy="selectin")


class AgentInstanceBinding(TimestampMixin, Base):
    __tablename__ = "agent_instance_bindings"
    __table_args__ = (UniqueConstraint("agent_id", "instance_id", name="uq_agent_instance_binding"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agents.id"), index=True)
    instance_id: Mapped[str] = mapped_column(String(36), ForeignKey("instances.id"), index=True)
    credential_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("instance_credentials.id", ondelete="RESTRICT"),
    )
    permission: Mapped[Permission] = mapped_column(
        Enum(Permission, native_enum=False, length=32),
        default=Permission.READWRITE,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    created_by_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"))

    agent: Mapped["Agent"] = relationship(lazy="selectin")
    instance: Mapped["Instance"] = relationship(back_populates="agent_bindings", lazy="selectin")
    credential: Mapped["InstanceCredential"] = relationship(lazy="selectin")
    created_by: Mapped["User"] = relationship(lazy="selectin")
    capabilities: Mapped[list["AgentInstanceBindingCapability"]] = relationship(
        back_populates="binding",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class AgentInstanceBindingCapability(Base):
    __tablename__ = "agent_instance_binding_capabilities"
    __table_args__ = (
        CheckConstraint(
            "capability IN ('db_instance:list', 'db_instance:describe', "
            "'db_instance:credentials:read', 'sql:read', 'sql:write')",
            name="ck_agent_instance_binding_capabilities_value",
        ),
    )

    binding_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agent_instance_bindings.id", ondelete="CASCADE"),
        primary_key=True,
    )
    capability: Mapped[BindingCapability] = mapped_column(
        _binding_capability_type(),
        primary_key=True,
    )

    binding: Mapped["AgentInstanceBinding"] = relationship(back_populates="capabilities", lazy="selectin")


class AgentProvisioningBinding(TimestampMixin, Base):
    __tablename__ = "agent_provisioning_bindings"
    __table_args__ = (UniqueConstraint("agent_id", "backend_id", name="uq_agent_provisioning_binding"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agents.id"), index=True)
    backend_id: Mapped[str] = mapped_column(String(36), ForeignKey("provisioning_backends.id"), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    created_by_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"))

    agent: Mapped["Agent"] = relationship(lazy="selectin")
    backend: Mapped["ProvisioningBackend"] = relationship(lazy="selectin")
    created_by: Mapped["User"] = relationship(lazy="selectin")
