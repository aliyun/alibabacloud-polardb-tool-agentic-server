from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from server.models.base import Base, TimestampMixin, generate_uuid


class OIDCLoginState(TimestampMixin, Base):
    __tablename__ = "oidc_login_states"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=generate_uuid
    )
    state_hash: Mapped[str] = mapped_column(String(64), index=True)
    purpose: Mapped[str] = mapped_column(String(32), index=True)
    initiator_user_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    config_revision: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    config_digest: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    nonce_ciphertext: Mapped[str] = mapped_column(Text)
    code_verifier_ciphertext: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    identity_snapshot_ciphertext: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    redirect_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    error_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
