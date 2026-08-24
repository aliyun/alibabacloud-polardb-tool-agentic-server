"""merge identity-source access and PolarRAG ACL mode heads"""

from collections.abc import Sequence


revision: str = "d5e6f7a8b9c0"
down_revision: tuple[str, str] | Sequence[str] | None = (
    "d3e4f5a6b7c8",
    "f5a6b7c8d9e0",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
