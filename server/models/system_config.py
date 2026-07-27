from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

from server.models.base import Base, TimestampMixin, generate_uuid


MAX_CONFIG_DOCUMENT_BYTES = 1_048_576
_large_text = Text().with_variant(mysql.LONGTEXT(), "mysql")


class SystemConfig(TimestampMixin, Base):
    __tablename__ = "system_config"

    config_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    config_value: Mapped[str] = mapped_column(_large_text, nullable=False)
    config_version: Mapped[int] = mapped_column(BigInteger, nullable=False)


class ConfigBootstrapClaim(TimestampMixin, Base):
    __tablename__ = "config_bootstrap_claim"

    singleton_key: Mapped[str] = mapped_column(
        String(32), primary_key=True, default="bootstrap"
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    failed_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    row_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )


class ConfigOperationReceipt(TimestampMixin, Base):
    __tablename__ = "config_operation_receipts"
    __table_args__ = (
        UniqueConstraint(
            "actor_scope",
            "idempotency_key_hash",
            name="uq_config_operation_receipt_actor_key",
        ),
        Index(
            "ix_config_operation_receipts_expires_at",
            "expires_at",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=generate_uuid
    )
    actor_scope: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key_hash: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    module: Mapped[str | None] = mapped_column(String(100), nullable=True)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    response_json: Mapped[str] = mapped_column(_large_text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

