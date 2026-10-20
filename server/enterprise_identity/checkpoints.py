"""Shared validation for provider-local directory sync checkpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def checkpoint_precedes_completed_sync(
    checkpoint: dict[str, Any], last_completed_at: datetime | None,
) -> bool:
    if last_completed_at is None:
        return False
    started_at = checkpoint.get("sync_started_at")
    if not isinstance(started_at, str):
        return True
    try:
        started = datetime.fromisoformat(started_at)
    except ValueError:
        return True
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    completed = last_completed_at
    if completed.tzinfo is None:
        completed = completed.replace(tzinfo=UTC)
    return started <= completed
