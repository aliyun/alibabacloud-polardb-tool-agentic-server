"""seed aliyun credential settings

Revision ID: 535a213beae6
Revises: 584da171771a
Create Date: 2026-06-22 14:22:31.174672

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from datetime import datetime, timezone
import uuid


# revision identifiers, used by Alembic.
revision: str = '535a213beae6'
down_revision: Union[str, Sequence[str], None] = '584da171771a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SETTINGS = [
    ("aliyun_credential_mode", "direct_ak", "Credential mode (direct_ak or assume_role)"),
    ("aliyun_access_key_id", "", "Access Key ID"),
    ("aliyun_access_key_secret", "", "Access Key Secret"),
    ("aliyun_role_arn", "", "RAM Role ARN (for assume_role mode)"),
    ("aliyun_role_session_name", "polardb-agentic", "Role session name"),
    ("aliyun_sts_duration_seconds", "3600", "STS token duration (seconds)"),
]


def upgrade() -> None:
    settings_table = sa.table(
        "system_settings",
        sa.column("id", sa.String),
        sa.column("key", sa.String),
        sa.column("value", sa.Text),
        sa.column("description", sa.Text),
        sa.column("created_at", sa.DateTime),
        sa.column("updated_at", sa.DateTime),
    )
    conn = op.get_bind()
    now = datetime.now(timezone.utc)
    for key, default, desc in SETTINGS:
        exists = conn.execute(
            sa.select(settings_table.c.id).where(settings_table.c.key == key)
        ).first()
        if exists:
            continue
        conn.execute(
            settings_table.insert().values(
                id=str(uuid.uuid4()),
                key=key,
                value=default,
                description=desc,
                created_at=now,
                updated_at=now,
            )
        )


def downgrade() -> None:
    settings_table = sa.table(
        "system_settings",
        sa.column("key", sa.String),
    )
    conn = op.get_bind()
    keys = [k for k, _, _ in SETTINGS]
    conn.execute(settings_table.delete().where(settings_table.c.key.in_(keys)))
