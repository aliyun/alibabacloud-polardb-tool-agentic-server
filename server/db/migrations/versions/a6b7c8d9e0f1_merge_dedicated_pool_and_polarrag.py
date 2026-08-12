"""merge Dedicated hot-pool and PolarRAG migration branches

Revision ID: a6b7c8d9e0f1
Revises: f3a4b5c6d7e8, c5d6e7f8a9b0
Create Date: 2026-08-12
"""

from collections.abc import Sequence


revision: str = "a6b7c8d9e0f1"
down_revision: str | Sequence[str] | None = (
    "f3a4b5c6d7e8",
    "c5d6e7f8a9b0",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
