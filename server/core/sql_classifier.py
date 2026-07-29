"""SQL type classifier — extracts statement type from the first keyword."""
from __future__ import annotations

_KNOWN_TYPES = frozenset({
    "SELECT", "INSERT", "UPDATE", "DELETE",
    "CREATE", "ALTER", "DROP", "TRUNCATE",
    "SHOW", "DESCRIBE", "EXPLAIN", "USE",
})


def _skip_leading_trivia(sql: str) -> int:
    index = 0
    length = len(sql)
    while index < length:
        while index < length and sql[index].isspace():
            index += 1
        if sql.startswith("--", index):
            newline = sql.find("\n", index + 2)
            if newline < 0:
                return length
            index = newline + 1
            continue
        if sql.startswith("/*", index):
            comment_end = sql.find("*/", index + 2)
            if comment_end < 0:
                return length
            index = comment_end + 2
            continue
        return index
    return length


def classify_sql(sql: str | None) -> str:
    """Extract SQL type from first keyword, skipping leading comments.

    Returns one of the known types or "OTHER".
    """
    if not sql or not sql.strip():
        return "OTHER"
    start = _skip_leading_trivia(sql)
    end = start
    while end < len(sql) and sql[end].isalpha():
        end += 1
    keyword = sql[start:end].upper()
    return keyword if keyword in _KNOWN_TYPES else "OTHER"
