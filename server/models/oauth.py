from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from server.models.base import Base, TimestampMixin, generate_uuid, utc_now


class OAuthRegisteredClient(TimestampMixin, Base):
    __tablename__ = "oauth_registered_clients"

    client_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    client_secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_id_issued_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    client_secret_expires_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    redirect_uris: Mapped[str] = mapped_column(Text)
    grant_types: Mapped[str] = mapped_column(Text)
    response_types: Mapped[str] = mapped_column(Text)
    token_endpoint_auth_method: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )
    scope: Mapped[str | None] = mapped_column(String(500), nullable=True)
    client_name: Mapped[str | None] = mapped_column(String(255), nullable=True)


class OAuthAuthorizationCode(TimestampMixin, Base):
    __tablename__ = "oauth_authorization_codes"

    code_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(255), index=True)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), index=True
    )
    redirect_uri: Mapped[str] = mapped_column(Text)
    redirect_uri_provided_explicitly: Mapped[bool] = mapped_column(
        Boolean, default=False
    )
    code_challenge: Mapped[str] = mapped_column(String(128))
    code_challenge_method: Mapped[str] = mapped_column(String(10))
    resource: Mapped[str | None] = mapped_column(Text, nullable=True)
    scopes: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class OAuthRefreshToken(TimestampMixin, Base):
    __tablename__ = "oauth_refresh_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(255), index=True)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), index=True
    )
    code_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    token_family: Mapped[str] = mapped_column(String(36), index=True)
    scopes: Mapped[str] = mapped_column(Text)
    resource: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class OAuthDeniedJTI(Base):
    __tablename__ = "oauth_denied_jtis"

    jti: Mapped[str] = mapped_column(String(36), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )


class OAuthPendingAuth(TimestampMixin, Base):
    __tablename__ = "oauth_pending_auths"

    session_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=generate_uuid
    )
    client_id: Mapped[str] = mapped_column(String(255))
    redirect_uri: Mapped[str] = mapped_column(Text)
    code_challenge: Mapped[str] = mapped_column(String(128))
    code_challenge_method: Mapped[str] = mapped_column(String(10))
    resource: Mapped[str | None] = mapped_column(Text, nullable=True)
    scopes: Mapped[str] = mapped_column(Text)
    state: Mapped[str | None] = mapped_column(Text, nullable=True)
    idp_state: Mapped[str | None] = mapped_column(String(36), nullable=True)
    idp_authorize_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    idp_code_verifier_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    idp_nonce: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class UserExternalIdentity(TimestampMixin, Base):
    __tablename__ = "user_external_identities"
    __table_args__ = (
        UniqueConstraint(
            "identity_provider", "external_subject", name="uq_idp_subject"
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=generate_uuid
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), index=True
    )
    identity_provider: Mapped[str] = mapped_column(String(100))
    external_subject: Mapped[str] = mapped_column(String(255))
