from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from server.models.base import Base, TimestampMixin, generate_uuid

if TYPE_CHECKING:
    from server.models.binding import DepartmentInstanceBinding, UserDepartment


class Department(TimestampMixin, Base):
    __tablename__ = "departments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    max_instances: Mapped[int | None] = mapped_column(Integer, nullable=True)
    agentic_db_cluster_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agentic_db_cluster_description: Mapped[str | None] = mapped_column(String(512), nullable=True)

    user_memberships: Mapped[list["UserDepartment"]] = relationship(back_populates="department", lazy="selectin")
    instance_bindings: Mapped[list["DepartmentInstanceBinding"]] = relationship(back_populates="department", lazy="selectin")
