"""SQL type classifier — extracts statement type from the first keyword."""
from __future__ import annotations

import re

_KNOWN_TYPES = frozenset({
    "SELECT", "INSERT", "UPDATE", "DELETE",
    "CREATE", "ALTER", "DROP", "TRUNCATE",
    "SHOW", "DESCRIBE", "EXPLAIN", "USE",
})

# Match line comments (-- ...\n) and block comments (/* ... */)
_COMMENT_RE = re.compile(r"(--[^\n]*\n|/\*.*?\*/)", re.DOTALL)


def classify_sql(sql: str | None) -> str:
    """Extract SQL type from first keyword, skipping leading comments.

    Returns one of the known types or "OTHER".
    """
    if not sql or not sql.strip():
        return "OTHER"
    stripped = _COMMENT_RE.sub("", sql).strip()
    if not stripped:
        return "OTHER"
    token = stripped.split(maxsplit=1)[0].upper()
    return token if token in _KNOWN_TYPES else "OTHER"
