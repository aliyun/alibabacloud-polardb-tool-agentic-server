"""merge enterprise identity and PolarRAG ACL mode heads"""

from collections.abc import Sequence

revision: str = "d3e4f5a6b7c8"
down_revision: tuple[str, str] | Sequence[str] | None = (
    "e4f5a6b7c8d9",
    "c2d3e4f5a6b7",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    pass

def downgrade() -> None:
    pass
