from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from server.models import ProvisioningOperationBudget


class RateLimited(RuntimeError):
    code = "RATE_LIMITED"


def hourly_window_start(now: datetime) -> datetime:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).replace(
        minute=0,
        second=0,
        microsecond=0,
    )


async def _ensure_budget_row(
    session: AsyncSession,
    *,
    scope_type: str,
    scope_id: str,
    operation: str,
    window_started_at: datetime,
) -> None:
    values = {
        "scope_type": scope_type,
        "scope_id": scope_id,
        "operation": operation,
        "window_started_at": window_started_at,
        "request_count": 0,
    }
    dialect = session.get_bind().dialect.name
    conflict_columns = [
        "scope_type",
        "scope_id",
        "operation",
        "window_started_at",
    ]
    if dialect == "sqlite":
        statement = (
            sqlite_insert(ProvisioningOperationBudget)
            .values(**values)
            .on_conflict_do_nothing(index_elements=conflict_columns)
        )
    elif dialect == "postgresql":
        statement = (
            postgresql_insert(ProvisioningOperationBudget)
            .values(**values)
            .on_conflict_do_nothing(index_elements=conflict_columns)
        )
    elif dialect in {"mysql", "mariadb"}:
        statement = mysql_insert(ProvisioningOperationBudget).values(
            **values
        ).prefix_with("IGNORE")
    else:
        existing = await session.scalar(
            select(ProvisioningOperationBudget.id).where(
                ProvisioningOperationBudget.scope_type == scope_type,
                ProvisioningOperationBudget.scope_id == scope_id,
                ProvisioningOperationBudget.operation == operation,
                ProvisioningOperationBudget.window_started_at
                == window_started_at,
            )
        )
        if existing is None:
            session.add(ProvisioningOperationBudget(**values))
            await session.flush()
        return
    await session.execute(statement)


async def consume_hourly_budget(
    session: AsyncSession,
    *,
    scope_type: str,
    scope_id: str,
    operation: str,
    limit: int,
    now: datetime,
) -> None:
    if limit <= 0:
        raise ValueError("Hourly budget limit must be positive")
    window_started_at = hourly_window_start(now)
    await _ensure_budget_row(
        session,
        scope_type=scope_type,
        scope_id=scope_id,
        operation=operation,
        window_started_at=window_started_at,
    )
    result = await session.execute(
        update(ProvisioningOperationBudget)
        .where(
            ProvisioningOperationBudget.scope_type == scope_type,
            ProvisioningOperationBudget.scope_id == scope_id,
            ProvisioningOperationBudget.operation == operation,
            ProvisioningOperationBudget.window_started_at
            == window_started_at,
            ProvisioningOperationBudget.request_count < limit,
        )
        .values(
            request_count=ProvisioningOperationBudget.request_count + 1
        )
    )
    if result.rowcount != 1:
        raise RateLimited("Hourly operation budget exceeded")
