from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.identity_federation import IdentityFederation
from server.config import OIDCConfig
from server.core.crypto import decrypt, encrypt
from server.models import OIDCLoginState

OIDC_LOGIN_PURPOSE_CONSOLE = "console_login"
OIDC_LOGIN_PURPOSE_CONFIG_TEST = "config_test"

OIDC_LOGIN_STATUS_PENDING = "pending"
OIDC_LOGIN_STATUS_EXCHANGING = "exchanging"
OIDC_LOGIN_STATUS_PASSED = "passed"
OIDC_LOGIN_STATUS_FAILED = "failed"
OIDC_LOGIN_STATUS_CONSUMED = "consumed"


@dataclass(frozen=True)
class ClaimedOIDCLogin:
    id: str
    purpose: str
    initiator_user_id: str | None
    config_revision: int | None
    config_digest: str | None
    nonce: str
    code_verifier: str | None
    redirect_path: str


def oidc_state_hash(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


def safe_login_redirect_path(path: str | None) -> str:
    if not path:
        return "/dashboard"
    parsed = urlsplit(path)
    if (
        parsed.scheme
        or parsed.netloc
        or not parsed.path.startswith("/")
        or parsed.path.startswith("//")
        or "\\" in path
    ):
        return "/dashboard"
    return path


async def create_oidc_login(
    session: AsyncSession,
    *,
    oidc_config: OIDCConfig,
    callback_url: str,
    purpose: str,
    redirect_path: str | None = None,
    initiator_user_id: str | None = None,
    config_revision: int | None = None,
    config_digest: str | None = None,
    expires_in: timedelta = timedelta(minutes=10),
) -> tuple[OIDCLoginState, str]:
    federation = IdentityFederation(
        oidc_config,
        provider_name=oidc_config.provider_name,
    )
    await federation.discover_endpoints()
    raw_state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    authorize_url, code_verifier = federation.build_authorize_url(
        callback_url,
        raw_state,
        nonce=nonce if oidc_config.protocol_mode == "oidc" else None,
    )
    record = OIDCLoginState(
        state_hash=oidc_state_hash(raw_state),
        purpose=purpose,
        initiator_user_id=initiator_user_id,
        config_revision=config_revision,
        config_digest=config_digest,
        nonce_ciphertext=encrypt(nonce),
        code_verifier_ciphertext=(
            encrypt(code_verifier) if code_verifier else None
        ),
        redirect_path=safe_login_redirect_path(redirect_path),
        status=OIDC_LOGIN_STATUS_PENDING,
        expires_at=datetime.now(timezone.utc) + expires_in,
    )
    session.add(record)
    await session.commit()
    return record, authorize_url


def _utc_now_comparable(dt: datetime) -> datetime:
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return now.replace(tzinfo=None)
    return now


async def claim_oidc_login(
    session: AsyncSession,
    raw_state: str,
) -> ClaimedOIDCLogin | None:
    state_hash = oidc_state_hash(raw_state)
    record = await session.scalar(
        select(OIDCLoginState).where(
            OIDCLoginState.state_hash == state_hash,
            OIDCLoginState.status == OIDC_LOGIN_STATUS_PENDING,
        )
    )
    if record is None:
        return None
    if record.expires_at < _utc_now_comparable(record.expires_at):
        record.status = OIDC_LOGIN_STATUS_FAILED
        record.error_code = "STATE_EXPIRED"
        await session.commit()
        return None

    claimed = await session.execute(
        update(OIDCLoginState)
        .where(
            OIDCLoginState.id == record.id,
            OIDCLoginState.state_hash == state_hash,
            OIDCLoginState.status == OIDC_LOGIN_STATUS_PENDING,
        )
        .values(status=OIDC_LOGIN_STATUS_EXCHANGING)
    )
    await session.commit()
    if claimed.rowcount != 1:  # type: ignore[attr-defined]
        return None

    return ClaimedOIDCLogin(
        id=record.id,
        purpose=record.purpose,
        initiator_user_id=record.initiator_user_id,
        config_revision=record.config_revision,
        config_digest=record.config_digest,
        nonce=decrypt(record.nonce_ciphertext),
        code_verifier=(
            decrypt(record.code_verifier_ciphertext)
            if record.code_verifier_ciphertext
            else None
        ),
        redirect_path=safe_login_redirect_path(record.redirect_path),
    )


async def fail_oidc_login(
    session: AsyncSession,
    state_id: str,
    error_code: str,
) -> None:
    await session.execute(
        update(OIDCLoginState)
        .where(
            OIDCLoginState.id == state_id,
            OIDCLoginState.status == OIDC_LOGIN_STATUS_EXCHANGING,
        )
        .values(
            status=OIDC_LOGIN_STATUS_FAILED,
            error_code=error_code,
            consumed_at=datetime.now(timezone.utc),
        )
    )
    await session.commit()


async def consume_console_oidc_login(
    session: AsyncSession,
    state_id: str,
) -> bool:
    consumed = await session.execute(
        update(OIDCLoginState)
        .where(
            OIDCLoginState.id == state_id,
            OIDCLoginState.status == OIDC_LOGIN_STATUS_EXCHANGING,
            OIDCLoginState.purpose == OIDC_LOGIN_PURPOSE_CONSOLE,
        )
        .values(
            status=OIDC_LOGIN_STATUS_CONSUMED,
            consumed_at=datetime.now(timezone.utc),
        )
    )
    return consumed.rowcount == 1  # type: ignore[attr-defined]


async def pass_config_test_oidc_login(
    session: AsyncSession,
    state_id: str,
    *,
    provider_name: str,
    subject: str,
) -> bool:
    snapshot = encrypt(
        json.dumps(
            {
                "provider_name": provider_name,
                "subject": subject,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    passed = await session.execute(
        update(OIDCLoginState)
        .where(
            OIDCLoginState.id == state_id,
            OIDCLoginState.status == OIDC_LOGIN_STATUS_EXCHANGING,
            OIDCLoginState.purpose == OIDC_LOGIN_PURPOSE_CONFIG_TEST,
        )
        .values(
            status=OIDC_LOGIN_STATUS_PASSED,
            identity_snapshot_ciphertext=snapshot,
        )
    )
    await session.commit()
    return passed.rowcount == 1  # type: ignore[attr-defined]
