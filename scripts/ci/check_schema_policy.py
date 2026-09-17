from __future__ import annotations

import ast
import re
import sys
from pathlib import Path


PROHIBITED_CALLS = {
    "CheckConstraint": "new check constraint",
    "ForeignKey": "new foreign key",
    "ForeignKeyConstraint": "new foreign key",
    "UniqueConstraint": "new unique constraint",
    "create_check_constraint": "new check constraint",
    "create_foreign_key": "new foreign key",
    "create_unique_constraint": "new unique constraint",
    "drop_column": "dropped column",
    "drop_constraint": "dropped constraint",
    "drop_index": "dropped index",
    "drop_table": "dropped table",
    "rename_table": "renamed table",
}

RAW_SQL_PATTERNS = (
    (re.compile(r"\bCREATE\s+(?:OR\s+REPLACE\s+)?VIEW\b", re.IGNORECASE), "new view"),
    (re.compile(r"\bCREATE\s+UNIQUE\s+INDEX\b", re.IGNORECASE), "new unique index"),
    (
        re.compile(r"\bADD\s+(?:CONSTRAINT\b.*)?(?:FOREIGN\s+KEY|UNIQUE|CHECK)\b", re.IGNORECASE),
        "new database constraint",
    ),
    (
        re.compile(r"\bDROP\s+(?:TABLE|COLUMN|INDEX|CONSTRAINT|VIEW)\b", re.IGNORECASE),
        "destructive raw SQL",
    ),
    (re.compile(r"\bRENAME\s+(?:TABLE|COLUMN)\b", re.IGNORECASE), "destructive raw SQL"),
    (
        re.compile(r"\bALTER\s+TABLE\b.*\b(?:DROP|RENAME|MODIFY|CHANGE)\b", re.IGNORECASE | re.DOTALL),
        "destructive raw SQL",
    ),
)


def _call_name(node: ast.Call) -> str | None:
    function = node.func
    if isinstance(function, ast.Attribute):
        return function.attr
    if isinstance(function, ast.Name):
        return function.id
    return None


def _keyword(node: ast.Call, name: str) -> ast.expr | None:
    for keyword in node.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _constant_bool(node: ast.expr | None, value: bool) -> bool:
    return isinstance(node, ast.Constant) and node.value is value


def _literal_string(node: ast.expr) -> str | None:
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, str) else None


class UpgradePolicyVisitor(ast.NodeVisitor):
    def __init__(self, helpers: dict[str, ast.AST] | None = None, aliases=None, external=None) -> None:
        self.violations: list[tuple[int, str]] = []
        self.helpers = helpers or {}
        self.aliases = aliases or {}
        self.external = external or set()
        self.visited_helpers: set[str] = set()

    def reject(self, node: ast.AST, reason: str) -> None:
        self.violations.append((getattr(node, "lineno", 1), reason))

    def visit_Call(self, node: ast.Call) -> None:
        name = _call_name(node)
        seen = set()
        while name in self.aliases and name not in seen:
            seen.add(name)
            name = self.aliases[name]
        root = node.func
        while isinstance(root, ast.Attribute):
            root = root.value
        if name in self.external or isinstance(root, ast.Name) and root.id in self.external:
            self.reject(node, "uninspectable imported migration helper")
        if not isinstance(node.func, (ast.Name, ast.Attribute)) or name in {
            "getattr",
            "setattr",
            "eval",
            "exec",
            "__import__",
        }:
            self.reject(node, "uninspectable dynamic migration call")
        if name in self.helpers and name not in self.visited_helpers:
            self.visited_helpers.add(name)
            self.visit(self.helpers[name])
        if name in PROHIBITED_CALLS:
            self.reject(node, PROHIBITED_CALLS[name])

        if name == "alter_column":
            forbidden_keywords = {
                "new_column_name": "renamed column",
                "type_": "changed column type",
                "server_default": "changed column default",
            }
            for keyword, reason in forbidden_keywords.items():
                if _keyword(node, keyword) is not None:
                    self.reject(node, reason)
            if _constant_bool(_keyword(node, "nullable"), False):
                self.reject(node, "tightened column nullability")

        if (
            name == "create_index"
            and _keyword(node, "unique") is not None
            and not _constant_bool(_keyword(node, "unique"), False)
        ):
            self.reject(node, "new unique index")

        if name == "add_column" and len(node.args) >= 2:
            column = node.args[1]
            if isinstance(column, ast.Call) and _call_name(column) == "Column":
                nullable = _keyword(column, "nullable")
                server_default = _keyword(column, "server_default")
                if _constant_bool(nullable, False) and server_default is None:
                    self.reject(column, "new non-null column without a database default")

        if name == "execute" and not node.args:
            self.reject(node, "uninspectable dynamic SQL")
        if name == "execute" and node.args:
            argument = node.args[0]
            if isinstance(argument, ast.Call) and _call_name(argument) == "text" and argument.args:
                argument = argument.args[0]
            sql = _literal_string(argument)
            if sql is None:
                self.reject(node, "uninspectable dynamic SQL")
            if sql is not None:
                for pattern, reason in RAW_SQL_PATTERNS:
                    if pattern.search(sql):
                        self.reject(node, reason)
                        break

        self.generic_visit(node)


def check(path: Path) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        return [(getattr(exc, "lineno", 1) or 1, f"cannot inspect migration: {exc}")]

    upgrade_functions = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "upgrade"
    ]
    scopes: list[ast.AST] = upgrade_functions or [tree]
    helpers = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name not in {"upgrade", "downgrade"}
    }
    aliases = {}
    external = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for item in node.names:
                alias = item.asname or item.name
                aliases[alias] = item.name
                if (node.module or "").split(".")[0] not in {"alembic", "sqlalchemy"}:
                    external.update({alias, item.name})
        elif isinstance(node, ast.Import):
            for item in node.names:
                if item.name.split(".")[0] not in {"alembic", "sqlalchemy"}:
                    external.add(item.asname or item.name.split(".")[0])
        elif isinstance(node, ast.Assign) and isinstance(node.value, (ast.Name, ast.Attribute)):
            value = node.value.id if isinstance(node.value, ast.Name) else node.value.attr
            for target in node.targets:
                if isinstance(target, ast.Name):
                    aliases[target.id] = value
    visitor = UpgradePolicyVisitor(helpers, aliases, external)
    for scope in scopes:
        visitor.visit(scope)
    return sorted(set(visitor.violations))


def main(argv: list[str]) -> int:
    failed = False
    for raw_path in argv[1:]:
        path = Path(raw_path)
        for line, reason in check(path):
            failed = True
            print(
                f"schema policy violation: {path}:{line}: {reason} is not allowed "
                "in a managed EXPAND migration upgrade()",
                file=sys.stderr,
            )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
