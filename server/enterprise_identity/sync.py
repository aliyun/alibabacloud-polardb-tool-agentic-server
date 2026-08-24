from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import select

from server.enterprise_identity.service import sync_identity_source
from server.models import (
    EnterpriseIdentitySource,
    EnterpriseIdentitySourceStatus,
    IdentitySourceProvider,
)


logger = logging.getLogger(__name__)


async def sync_configured_identity_sources_once(
    session_factory: Any,
    *,
    feishu_client_factory: Any = None,
    sharepoint_client_factory: Any = None,
    acl_snapshot_client_factory: Any = None,
) -> None:
    """Synchronize sources independently so one provider failure is isolated."""
    async with session_factory() as session:
        source_ids = list(
            (
                await session.execute(
                    select(EnterpriseIdentitySource.id)
                    .where(
                        EnterpriseIdentitySource.config_ciphertext.is_not(None),
                        EnterpriseIdentitySource.tenant_id.is_not(None),
                        EnterpriseIdentitySource.provider.in_(
                            (
                                IdentitySourceProvider.FEISHU,
                                IdentitySourceProvider.SHAREPOINT,
                            )
                        ),
                        EnterpriseIdentitySource.status.in_(
                            (
                                EnterpriseIdentitySourceStatus.PENDING_BINDING,
                                EnterpriseIdentitySourceStatus.ACTIVE,
                                EnterpriseIdentitySourceStatus.STALE,
                            )
                        ),
                    )
                    .order_by(EnterpriseIdentitySource.id)
                )
            ).scalars()
        )

    for source_id in source_ids:
        async with session_factory() as session:
            source = await session.get(EnterpriseIdentitySource, source_id)
            if (
                source is None
                or source.tenant_id is None
                or source.status
                not in {
                    EnterpriseIdentitySourceStatus.PENDING_BINDING,
                    EnterpriseIdentitySourceStatus.ACTIVE,
                    EnterpriseIdentitySourceStatus.STALE,
                }
            ):
                continue
            try:
                await sync_identity_source(
                    session,
                    source,
                    feishu_client_factory=feishu_client_factory,
                    sharepoint_client_factory=sharepoint_client_factory,
                    acl_snapshot_client_factory=acl_snapshot_client_factory,
                )
            except Exception as exc:
                await session.commit()
                logger.warning(
                    "enterprise_identity.source.sync_failed",
                    extra={
                        "identity_source_id": source_id,
                        "provider": source.provider.value,
                        "error_type": type(exc).__name__,
                    },
                )
            else:
                await session.commit()


async def identity_source_sync_loop(
    session_factory: Any,
    *,
    interval_seconds: float = 300.0,
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        await sync_configured_identity_sources_once(session_factory)
