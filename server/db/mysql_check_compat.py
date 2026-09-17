from __future__ import annotations

import logging
from typing import Any

import sqlalchemy as sa
from alembic.ddl.mysql import MySQLImpl

_MYSQL_CHECK_CONSTRAINT_MIN_VERSION = (8, 0, 16)

logger = logging.getLogger(__name__)


def supports_check_constraints(dialect: Any) -> bool:
    """Return whether the connected database stores and enforces CHECK DDL."""
    if dialect.name != "mysql":
        return True
    version = getattr(dialect, "server_version_info", None)
    if not version:
        # Offline migrations cannot discover the target server version. Keep
        # emitting normal DDL so generated SQL does not silently lose checks.
        return True
    return tuple(version[:3]) >= _MYSQL_CHECK_CONSTRAINT_MIN_VERSION


class CompatibleMySQLImpl(MySQLImpl):
    """Skip only unsupported CHECK DDL on pre-8.0.16 MySQL servers."""

    __dialect__ = "mysql"

    def add_constraint(self, const: Any, **kw: Any) -> None:
        if isinstance(const, sa.CheckConstraint) and not (
            supports_check_constraints(self.dialect)
        ):
            logger.warning(
                "skipping unsupported MySQL CHECK constraint creation: %s",
                const.name,
            )
            return
        super().add_constraint(const, **kw)

    def drop_constraint(self, const: Any, **kw: Any) -> None:
        if isinstance(const, sa.CheckConstraint) and not (
            supports_check_constraints(self.dialect)
        ):
            logger.warning(
                "skipping unsupported MySQL CHECK constraint removal: %s",
                const.name,
            )
            return
        super().drop_constraint(const, **kw)
