from __future__ import annotations

import hashlib
from decimal import Decimal

from server.core.provisioning_backend_repository import BackendCandidate


def tie_break_score(agent_id: str, client_token: str, backend_id: str) -> str:
    raw = f"{agent_id}:{client_token}:{backend_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def candidate_sort_key(
    item: BackendCandidate,
    agent_id: str,
    client_token: str,
) -> tuple[int, Decimal, str]:
    load = Decimal(item.active_count) / Decimal(item.max_active_resources)
    return (
        -item.priority,
        load,
        tie_break_score(agent_id, client_token, item.backend_id),
    )


def order_candidates(
    candidates: list[BackendCandidate],
    *,
    agent_id: str,
    client_token: str,
) -> list[BackendCandidate]:
    return sorted(
        candidates,
        key=lambda item: candidate_sort_key(item, agent_id, client_token),
    )
