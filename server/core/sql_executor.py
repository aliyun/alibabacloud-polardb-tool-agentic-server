from __future__ import annotations

import enum
import logging
import re
import time

import sqlparse
from sqlparse import tokens as sql_tokens

from server.config import get_config

logger = logging.getLogger(__name__)


class SQLSecurityError(Exception):
    def __init__(self, message: str, code: str = "BLOCKED_SQL"):
        self.message = message
        self.code = code
        super().__init__(message)


class RateLimitError(Exception):
    def __init__(self, message: str = "Too many requests. Please slow down."):
        self.message = message
        self.code = "RATE_LIMITED"
        super().__init__(message)


class SQLExecutionError(Exception):
    def __init__(self, message: str, code: str = "SQL_ERROR"):
        self.message = message
        self.code = code
        super().__init__(message)


class SQLRiskLevel(str, enum.Enum):
    SAFE = "safe"
    DESTRUCTIVE = "destructive"
    BLOCKED = "blocked"


# Pod-local per-user rate limiter (token bucket)
_rate_limiters: dict[str, dict] = {}
_RATE_LIMITER_TTL = 600


def _check_rate_limit(user_id: str) -> None:
    config = get_config().sql_security.rate_limit
    if not config.enabled:
        return

    now = time.time()

    expired = [k for k, v in _rate_limiters.items() if now - v["last_refill"] > _RATE_LIMITER_TTL]
    for k in expired:
        del _rate_limiters[k]

    if user_id not in _rate_limiters:
        _rate_limiters[user_id] = {
            "tokens": config.burst,
            "last_refill": now,
        }

    limiter = _rate_limiters[user_id]
    elapsed = now - limiter["last_refill"]
    refill = elapsed * (config.requests_per_minute / 60.0)
    limiter["tokens"] = min(config.burst, limiter["tokens"] + refill)
    limiter["last_refill"] = now

    if limiter["tokens"] < 1:
        raise RateLimitError()

    limiter["tokens"] -= 1


def check_sql_safety(sql: str) -> None:
    """Check SQL statement against safety rules. Raises SQLSecurityError if blocked.

    Treats both BLOCKED and DESTRUCTIVE as blocked for backward compatibility.
    For the two-phase confirmation flow, use classify_sql_risk() directly.
    """
    risk = classify_sql_risk(sql)
    if risk in (SQLRiskLevel.BLOCKED, SQLRiskLevel.DESTRUCTIVE):
        raise SQLSecurityError(
            "This SQL statement is not allowed by security policy.",
            "BLOCKED_SQL",
        )


def _is_executable_comment(value: str) -> bool:
    return value.lstrip().startswith("/*!")


def _executable_comment_sql(value: str) -> str:
    leading = value[:len(value) - len(value.lstrip())]
    body = value.lstrip()
    if not body.startswith("/*!") or not body.endswith("*/"):
        return value
    content = re.sub(r"^\s*\d{5,6}\s*", "", body[3:-2], count=1)
    return f"{leading} {content} "


def expand_executable_comments(sql: str) -> str:
    try:
        parts = []
        for stmt in sqlparse.parse(sql):
            for token in stmt.flatten():
                if token.ttype in sql_tokens.Comment and _is_executable_comment(token.value):
                    parts.append(_executable_comment_sql(token.value))
                else:
                    parts.append(token.value)
        return "".join(parts)
    except Exception:
        return sql


def _has_executable_text(statement: str) -> bool:
    try:
        parsed = sqlparse.parse(statement)
        if not parsed:
            return False
        for stmt in parsed:
            for token in stmt.flatten():
                if token.is_whitespace:
                    continue
                if token.ttype is sql_tokens.Punctuation and token.value == ";":
                    continue
                if token.ttype in sql_tokens.Comment and not _is_executable_comment(token.value):
                    continue
                return True
    except Exception:
        stripped = statement.strip().strip(";").strip()
        return bool(stripped)
    return False


def _statement_texts(sql: str) -> list[str]:
    try:
        sql = expand_executable_comments(sql)
        return [
            stmt for stmt in sqlparse.split(sql)
            if _has_executable_text(stmt)
        ]
    except Exception:
        stripped = sql.strip().strip(";").strip()
        return [stripped] if stripped else []


def has_sql_statement(sql: str) -> bool:
    return bool(_statement_texts(sql))


def has_multiple_statements(sql: str) -> bool:
    return len(_statement_texts(sql)) > 1


def _single_statement_text(sql: str) -> str | None:
    statements = _statement_texts(sql)
    if len(statements) != 1:
        return None
    return statements[0]


def is_select(sql: str) -> bool:
    """Check if SQL is a single SELECT statement."""
    sql = _single_statement_text(sql) or ""
    if not sql:
        return False
    try:
        parsed = sqlparse.parse(sql)
        if parsed:
            return bool(parsed[0].get_type() == "SELECT")
    except Exception:
        pass
    return False


def _first_executable_token(sql: str) -> str | None:
    try:
        for stmt in sqlparse.parse(sql):
            for token in stmt.flatten():
                if token.is_whitespace:
                    continue
                if token.ttype is sql_tokens.Punctuation and token.value == ";":
                    continue
                if token.ttype in sql_tokens.Comment:
                    continue
                return str(token.normalized).upper()
    except Exception:
        pass
    return None


def is_readonly_sql(sql: str) -> bool:
    """Check if SQL is a read-only statement (SELECT, SHOW, DESCRIBE, EXPLAIN).

    Used to enforce READONLY permission at the application level: non-read-only
    SQL (INSERT, UPDATE, DELETE, CREATE, ALTER, DROP, etc.) is rejected for
    users with READONLY permission.
    """
    if is_select(sql):
        return True
    sql = _single_statement_text(sql) or ""
    if not sql:
        return False
    # SHOW, DESCRIBE/DESC, EXPLAIN are all read-only.
    return _first_executable_token(sql) in {"SHOW", "DESCRIBE", "DESC", "EXPLAIN"}


def _has_limit_clause(sql: str) -> bool:
    try:
        for stmt in sqlparse.parse(sql):
            for token in stmt.flatten():
                if token.ttype is sqlparse.tokens.Keyword and token.normalized == "LIMIT":
                    return True
    except Exception:
        pass
    return False


def _strip_trailing_non_executable_tokens(sql: str) -> str:
    try:
        parsed = sqlparse.parse(sql)
        if len(parsed) != 1:
            return sql.strip().rstrip(";").strip()
        tokens = list(parsed[0].flatten())
        while tokens:
            token = tokens[-1]
            if token.is_whitespace:
                tokens.pop()
                continue
            if token.ttype in sql_tokens.Comment and not _is_executable_comment(token.value):
                tokens.pop()
                continue
            if token.ttype is sql_tokens.Punctuation and token.value == ";":
                tokens.pop()
                continue
            break
        return "".join(token.value for token in tokens).strip()
    except Exception:
        return sql.strip().rstrip(";").strip()


def apply_pagination(sql: str, max_rows: int, offset: int = 0) -> str:
    """Append LIMIT/OFFSET to SELECT queries if not already present."""
    if not is_select(sql):
        return sql
    sql = _single_statement_text(sql) or sql
    if not _has_limit_clause(sql):
        sql = f"{_strip_trailing_non_executable_tokens(sql)} LIMIT {int(max_rows)} OFFSET {int(offset)}"
    return sql


def reset_rate_limiters() -> None:
    """Clear rate limiter state (for testing)."""
    _rate_limiters.clear()


def _has_where_clause(stmt) -> bool:
    for token in stmt.flatten():
        if token.ttype is sqlparse.tokens.Keyword and token.normalized == "WHERE":
            return True
    return False


_TAUTOLOGICAL_PATTERNS = {"1=1", "true", "1"}
_SELF_COMPARISON_OPERATORS = {"=", "<=>"}


def _where_token_values(tokens: list) -> list[str]:
    values = []
    for token in tokens:
        if token.is_whitespace:
            continue
        if token.ttype in sql_tokens.Comment:
            continue
        if token.ttype is sql_tokens.Punctuation and token.value == ";":
            continue
        if token.ttype in sql_tokens.String:
            values.append("?")
        else:
            values.append(str(token.normalized).lower())
    return values


def _strip_edge_parentheses(values: list[str]) -> list[str]:
    values = list(values)
    while values and values[0] == "(":
        values = values[1:]
    while values and values[-1] == ")":
        values = values[:-1]
    return values


def _strip_balanced_parentheses(value: str) -> str:
    while value.startswith("(") and value.endswith(")"):
        depth = 0
        balanced = True
        for index, char in enumerate(value):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0 and index != len(value) - 1:
                    balanced = False
                    break
            if depth < 0:
                balanced = False
                break
        if not balanced or depth != 0:
            break
        value = value[1:-1].strip()
    return value


def _is_tautological_token_values(values: list[str]) -> bool:
    normalized = "".join(_strip_edge_parentheses(values))
    if normalized in _TAUTOLOGICAL_PATTERNS:
        return True
    values = _strip_edge_parentheses(values)
    operators = [
        (index, value)
        for index, value in enumerate(values)
        if value in _SELF_COMPARISON_OPERATORS
    ]
    if len(operators) != 1:
        return False
    operator_index, _operator = operators[0]
    left = values[:operator_index]
    right = values[operator_index + 1:]
    if not left or not right or "?" in left or "?" in right:
        return False
    return left == right


def _has_or_tautology(values: list[str]) -> bool:
    for index, value in enumerate(values):
        if value != "or":
            continue
        if _is_tautological_token_values(values[:index]):
            return True
        if _is_tautological_token_values(values[index + 1:]):
            return True
    return False


def _has_tautological_where(stmt) -> bool:
    tokens = list(stmt.flatten())
    for i, token in enumerate(tokens):
        if token.ttype is sqlparse.tokens.Keyword and token.normalized == "WHERE":
            where_tokens = tokens[i + 1:]
            rest = "".join(
                t.value for t in where_tokens
                if t.ttype not in sql_tokens.Comment
            ).strip().rstrip(";").strip()
            normalized = re.sub(r"\s+", "", _strip_balanced_parentheses(rest).lower())
            if normalized in _TAUTOLOGICAL_PATTERNS:
                return True
            where_values = _where_token_values(where_tokens)
            if _is_tautological_token_values(where_values):
                return True
            return _has_or_tautology(where_values)
    return False


def classify_sql_risk(sql: str) -> SQLRiskLevel:
    config = get_config().sql_security
    sql = "; ".join(_statement_texts(sql))
    if not sql:
        return SQLRiskLevel.SAFE

    # Layer 1: keyword blacklist (e.g. DROP DATABASE) -> BLOCKED
    try:
        all_tokens = []
        for stmt in sqlparse.parse(sql):
            all_tokens.extend(stmt.flatten())
        keyword_str = " ".join(
            t.normalized for t in all_tokens
            if t.ttype in (sqlparse.tokens.Keyword, sqlparse.tokens.Keyword.DDL, sqlparse.tokens.Keyword.DML)
        )
        for kw in config.blocked_keywords:
            if kw.upper() in keyword_str:
                return SQLRiskLevel.BLOCKED
    except Exception:
        pass

    # Layer 2: statement type analysis -> DESTRUCTIVE or SAFE
    try:
        for stmt in sqlparse.parse(sql):
            stmt_type = stmt.get_type()
            if stmt_type is None:
                for token in stmt.flatten():
                    if token.ttype in (sqlparse.tokens.Keyword.DDL, sqlparse.tokens.Keyword.DML):
                        stmt_type = token.normalized
                        break

            if stmt_type and stmt_type.upper() in [t.upper() for t in config.confirmable_statement_types]:
                if stmt_type.upper() == "DELETE" and _has_where_clause(stmt) and not _has_tautological_where(stmt):
                    continue
                return SQLRiskLevel.DESTRUCTIVE
    except Exception:
        pass

    return SQLRiskLevel.SAFE


def encode_cursor(offset: int) -> str:
    import base64
    import json
    return base64.urlsafe_b64encode(json.dumps({"offset": offset}).encode()).decode()
