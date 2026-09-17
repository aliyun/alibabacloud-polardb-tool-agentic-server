from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts" / "ci" / "check-schema-policy.sh"


def _run_checker(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(CHECKER), str(path)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_schema_policy_accepts_additive_revision(tmp_path: Path) -> None:
    migration = tmp_path / "safe.py"
    migration.write_text(
        """
def upgrade():
    op.add_column("users", sa.Column("state", sa.String(32)))
    op.add_column(
        "users",
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade():
    op.drop_column("users", "enabled")
    op.drop_column("users", "state")
""",
        encoding="utf-8",
    )

    result = _run_checker(migration)

    assert result.returncode == 0, result.stderr


def test_schema_policy_checks_called_helpers(tmp_path: Path) -> None:
    migration = tmp_path / 'helper.py'
    migration.write_text('def remove_old():\n    op.drop_table("legacy")\ndef upgrade():\n    remove_old()\n')
    result = _run_checker(migration)
    assert result.returncode != 0
    assert 'dropped table' in result.stderr


def test_schema_policy_rejects_uninspectable_sql(tmp_path: Path) -> None:
    migration = tmp_path / 'dynamic.py'
    migration.write_text('def upgrade():\n    op.execute(make_sql())\n')
    result = _run_checker(migration)
    assert result.returncode != 0

def test_schema_policy_rejects_new_foreign_key(tmp_path: Path) -> None:
    migration = tmp_path / "unsafe_fk.py"
    migration.write_text(
        'sa.ForeignKeyConstraint(["user_id"], ["users.id"])\n',
        encoding="utf-8",
    )

    result = _run_checker(migration)

    assert result.returncode != 0
    assert "new foreign key" in result.stderr.lower()


def test_schema_policy_rejects_new_view(tmp_path: Path) -> None:
    migration = tmp_path / "unsafe_view.py"
    migration.write_text(
        'op.execute("CREATE OR REPLACE VIEW current_users AS SELECT 1")\n',
        encoding="utf-8",
    )

    result = _run_checker(migration)

    assert result.returncode != 0
    assert "new view" in result.stderr.lower()


def test_schema_policy_rejects_new_unique_and_check_constraints(tmp_path: Path) -> None:
    migration = tmp_path / "unsafe_constraints.py"
    migration.write_text(
        """
def upgrade():
    op.create_unique_constraint("uq_users_name", "users", ["name"])
    op.create_check_constraint("ck_users_name", "users", "name <> ''")
""",
        encoding="utf-8",
    )

    result = _run_checker(migration)

    assert result.returncode != 0
    assert "new unique constraint" in result.stderr.lower()
    assert "new check constraint" in result.stderr.lower()


def test_schema_policy_rejects_destructive_upgrade_operations(tmp_path: Path) -> None:
    migration = tmp_path / "unsafe_drop.py"
    migration.write_text(
        """
def upgrade():
    op.drop_column("users", "legacy_name")
    op.drop_index("ix_users_legacy_name", table_name="users")


def downgrade():
    op.add_column("users", sa.Column("legacy_name", sa.String(32)))
""",
        encoding="utf-8",
    )

    result = _run_checker(migration)

    assert result.returncode != 0
    assert "dropped column" in result.stderr.lower()
    assert "dropped index" in result.stderr.lower()


def test_schema_policy_rejects_incompatible_alter_column(tmp_path: Path) -> None:
    migration = tmp_path / "unsafe_alter.py"
    migration.write_text(
        """
def upgrade():
    op.alter_column(
        "users",
        "display_name",
        new_column_name="name",
        type_=sa.String(32),
        nullable=False,
        server_default=None,
    )
""",
        encoding="utf-8",
    )

    result = _run_checker(migration)

    assert result.returncode != 0
    assert "renamed column" in result.stderr.lower()
    assert "changed column type" in result.stderr.lower()
    assert "tightened column nullability" in result.stderr.lower()
    assert "changed column default" in result.stderr.lower()


def test_schema_policy_rejects_raw_destructive_sql(tmp_path: Path) -> None:
    migration = tmp_path / "unsafe_sql.py"
    migration.write_text(
        """
def upgrade():
    op.execute("ALTER TABLE users DROP COLUMN legacy_name")
""",
        encoding="utf-8",
    )

    result = _run_checker(migration)

    assert result.returncode != 0
    assert "destructive raw sql" in result.stderr.lower()


def test_schema_policy_rejects_new_non_null_column_without_default(tmp_path: Path) -> None:
    migration = tmp_path / "unsafe_not_null.py"
    migration.write_text(
        """
def upgrade():
    op.add_column(
        "users",
        sa.Column("enabled", sa.Boolean(), nullable=False),
    )
""",
        encoding="utf-8",
    )

    result = _run_checker(migration)

    assert result.returncode != 0
    assert "without a database default" in result.stderr.lower()


def test_schema_policy_tolerates_historical_migrations() -> None:
    result = subprocess.run(
        [str(CHECKER)],
        cwd=ROOT,
        env={**os.environ, "SCHEMA_POLICY_BASE": "HEAD"},
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_schema_policy_rejects_indirect_destructive_calls(tmp_path: Path) -> None:
    examples = (
        'from custom_helpers import remove\ndef upgrade():\n    remove()\n',
        'drop = op.drop_table\ndef upgrade():\n    drop("legacy")\n',
        'def upgrade():\n    getattr(op, "drop_table")("legacy")\n',
        'def upgrade():\n    op.execute(statement=make_sql())\n',
        'def upgrade():\n    op.create_index("x", "t", ["a"], unique=flag)\n',
    )
    for index, example in enumerate(examples):
        path = tmp_path / f'indirect_{index}.py'
        path.write_text(example)
        assert _run_checker(path).returncode != 0, example
