"""merge Dedicated hot-pool and upload cleanup migration branches

Revision ID: a9b0c1d2e3f4
Revises: a6b7c8d9e0f1, f8a9b0c1d2e3
Create Date: 2026-08-12
"""

from collections.abc import Sequence


revision: str = "a9b0c1d2e3f4"
down_revision: str | Sequence[str] | None = (
    "a6b7c8d9e0f1",
    "f8a9b0c1d2e3",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
