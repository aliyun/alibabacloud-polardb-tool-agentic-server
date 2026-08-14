"""snapshot PolarRAG upload cleanup credentials

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e7f8a9b0c1d2"
down_revision: str | Sequence[str] | None = "d6e7f8a9b0c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "polarrag_upload_cleanups",
        sa.Column("oss_access_key_id_ciphertext", sa.Text(), nullable=True),
    )
    op.add_column(
        "polarrag_upload_cleanups",
        sa.Column(
            "oss_access_key_secret_ciphertext",
            sa.Text(),
            nullable=True,
        ),
    )
    op.execute(
        sa.text(
            """
            UPDATE polarrag_upload_cleanups
               SET oss_access_key_id_ciphertext = (
                       SELECT s.oss_access_key_id_ciphertext
                         FROM polarrag_spaces AS s
                        WHERE s.knowledge_space_id =
                              polarrag_upload_cleanups.knowledge_space_id
                   ),
                   oss_access_key_secret_ciphertext = (
                       SELECT s.oss_access_key_secret_ciphertext
                         FROM polarrag_spaces AS s
                        WHERE s.knowledge_space_id =
                              polarrag_upload_cleanups.knowledge_space_id
                   )
            """
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("polarrag_upload_cleanups") as batch_op:
        batch_op.drop_column("oss_access_key_secret_ciphertext")
        batch_op.drop_column("oss_access_key_id_ciphertext")
