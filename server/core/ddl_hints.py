"""DDL hint detection for cross-session semantic continuity.

Detects CREATE TABLE and ALTER TABLE ADD/MODIFY COLUMN statements,
and determines whether to suggest adding COMMENTs.
"""
from __future__ import annotations

import sqlparse
from sqlparse.tokens import DDL, Keyword

from server.core.sql_executor import expand_executable_comments

DDL_COMMENT_HINT = (
    "💡 Consider adding COMMENT to describe this table's purpose and column meanings "
    "for future reference. Example: ALTER TABLE <table> COMMENT 'description';"
)


def should_add_comment_hint(sql: str) -> bool:
    """Return True if this SQL is a DDL that would benefit from a COMMENT hint.

    Rules:
    - Must be CREATE TABLE or ALTER TABLE ADD/MODIFY COLUMN
    - Must NOT already contain the COMMENT keyword anywhere in the statement
    - Uses sqlparse to avoid false positives from string literals
    """
    try:
        sql = expand_executable_comments(sql)
        parsed = sqlparse.parse(sql)
        if not parsed:
            return False

        stmt = parsed[0]
        stmt_type = stmt.get_type()

        # Fallback: extract DDL type from tokens if get_type() returns None
        if stmt_type is None or stmt_type == "UNKNOWN":
            for token in stmt.flatten():
                if token.ttype in (DDL, Keyword.DDL):
                    stmt_type = token.normalized
                    break

        if stmt_type is None:
            return False

        stmt_upper = stmt_type.upper()

        # Only care about CREATE and ALTER
        if stmt_upper not in ("CREATE", "ALTER"):
            return False

        # Collect keyword tokens (excludes string literals & identifiers)
        keywords = [
            t.normalized.upper()
            for t in stmt.flatten()
            if t.ttype in (Keyword, DDL, Keyword.DDL, Keyword.DML)
        ]

        is_table_ddl = False
        if stmt_upper == "CREATE" and "TABLE" in keywords:
            is_table_ddl = True
        elif stmt_upper == "ALTER" and "TABLE" in keywords:
            # Only ADD COLUMN or MODIFY COLUMN
            if "ADD" in keywords or "MODIFY" in keywords:
                is_table_ddl = True

        if not is_table_ddl:
            return False

        # Check if COMMENT keyword already present (in non-string tokens)
        if "COMMENT" in keywords:
            return False

        return True

    except Exception:
        return False
