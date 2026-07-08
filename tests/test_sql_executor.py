import pytest

from server.config import reset_config
from server.core.sql_executor import (
    check_sql_safety, apply_pagination, _check_rate_limit, is_select,
    classify_sql_risk, SQLRiskLevel,
    SQLSecurityError, RateLimitError, reset_rate_limiters,
    has_multiple_statements, has_sql_statement, is_readonly_sql,
)


@pytest.fixture(autouse=True)
def clean():
    reset_config()
    reset_rate_limiters()
    yield
    reset_config()
    reset_rate_limiters()


class TestSQLSafety:
    def test_select_allowed(self):
        check_sql_safety("SELECT * FROM users")

    def test_insert_allowed(self):
        check_sql_safety("INSERT INTO users (name) VALUES ('test')")

    def test_drop_blocked(self):
        with pytest.raises(SQLSecurityError):
            check_sql_safety("DROP TABLE users")

    def test_truncate_blocked(self):
        with pytest.raises(SQLSecurityError):
            check_sql_safety("TRUNCATE TABLE users")

    def test_alter_blocked(self):
        with pytest.raises(SQLSecurityError):
            check_sql_safety("ALTER TABLE users ADD COLUMN x INT")

    def test_drop_database_keyword_blocked(self):
        with pytest.raises(SQLSecurityError):
            check_sql_safety("DROP DATABASE mydb")

    def test_case_insensitive(self):
        with pytest.raises(SQLSecurityError):
            check_sql_safety("drop table users")


class TestPagination:
    def test_adds_limit_to_select(self):
        result = apply_pagination("SELECT * FROM users", 100)
        assert "LIMIT 100" in result

    def test_does_not_add_to_insert(self):
        sql = "INSERT INTO users VALUES (1)"
        assert apply_pagination(sql, 100) == sql

    def test_does_not_add_to_show_branches(self):
        sql = "SHOW BRANCHES"
        assert apply_pagination(sql, 100) == sql

    def test_does_not_duplicate_limit(self):
        sql = "SELECT * FROM users LIMIT 10"
        result = apply_pagination(sql, 100)
        assert result.count("LIMIT") == 1

    def test_offset(self):
        result = apply_pagination("SELECT * FROM users", 100, offset=50)
        assert "OFFSET 50" in result

    def test_does_not_paginate_multiple_statements(self):
        sql = "SELECT 1; USE db1"
        assert apply_pagination(sql, 100) == sql

    def test_paginates_single_select_after_leading_comment_statement(self):
        sql = "/* comment */; SELECT * FROM users"
        assert apply_pagination(sql, 100) == "SELECT * FROM users LIMIT 100 OFFSET 0"

    def test_paginates_single_select_before_trailing_comment_statement(self):
        sql = "SELECT * FROM users; /* comment */"
        assert apply_pagination(sql, 100) == "SELECT * FROM users LIMIT 100 OFFSET 0"

    def test_paginates_single_select_before_trailing_line_comment(self):
        sql = "SELECT * FROM users; -- comment\n"
        assert apply_pagination(sql, 100) == "SELECT * FROM users LIMIT 100 OFFSET 0"

    def test_paginates_single_select_before_trailing_hash_comment(self):
        sql = "SELECT * FROM users; # comment\n"
        assert apply_pagination(sql, 100) == "SELECT * FROM users LIMIT 100 OFFSET 0"

    def test_paginates_single_select_with_trailing_line_comment_without_semicolon(self):
        sql = "SELECT * FROM users -- comment\n"
        assert apply_pagination(sql, 100) == "SELECT * FROM users LIMIT 100 OFFSET 0"

    def test_executable_comment_like_string_literal_is_preserved(self):
        sql = "SELECT '/*!50000 DROP TABLE users */'"
        assert apply_pagination(sql, 100) == "SELECT '/*!50000 DROP TABLE users */' LIMIT 100 OFFSET 0"


class TestIsSelect:
    def test_select(self):
        assert is_select("SELECT * FROM users") is True

    def test_insert(self):
        assert is_select("INSERT INTO users VALUES (1)") is False

    def test_multiple_statements_not_select(self):
        assert is_select("SELECT 1; USE db1") is False

    def test_comment_statement_then_select_is_select(self):
        assert is_select("/* comment */; SELECT 1") is True


class TestStatementCount:
    def test_has_sql_statement(self):
        assert has_sql_statement("SELECT 1")
        assert has_sql_statement("SELECT 1;")
        assert has_sql_statement("/* comment */ SELECT 1")
        assert has_sql_statement("-- comment\nSELECT 1")
        assert has_sql_statement("/*!50000 SELECT 1 */")
        assert not has_sql_statement("")
        assert not has_sql_statement("   ")
        assert not has_sql_statement(";")
        assert not has_sql_statement(" ; ; ")
        assert not has_sql_statement("/* comment */")
        assert not has_sql_statement("-- comment")
        assert not has_sql_statement("# comment")

    def test_single_statement(self):
        assert not has_multiple_statements("SELECT 1")
        assert not has_multiple_statements("SELECT 1;")
        assert not has_multiple_statements("SELECT ';'")
        assert not has_multiple_statements("SELECT '/*!50000 DROP TABLE users */'")
        assert not has_multiple_statements("/* comment */; SELECT 1")
        assert not has_multiple_statements("SELECT 1; /* comment */")

    def test_multiple_statements(self):
        assert has_multiple_statements("SELECT 1; USE db1")
        assert has_multiple_statements("SELECT 'USE db1'; USE db2")
        assert has_multiple_statements("SELECT 1; /* comment */; USE db1")

    def test_trailing_empty_statements_ignored(self):
        assert not has_multiple_statements("SELECT 1;;")


class TestRateLimiter:
    def test_allows_normal_requests(self):
        for _ in range(10):
            _check_rate_limit("user-1")

    def test_blocks_excessive_requests(self):
        for _ in range(10):
            _check_rate_limit("user-2")
        with pytest.raises(RateLimitError):
            _check_rate_limit("user-2")

    def test_different_users_independent(self):
        for _ in range(10):
            _check_rate_limit("user-3")
        _check_rate_limit("user-4")  # should not be limited


class TestClassifySQLRisk:
    def test_select_is_safe(self):
        assert classify_sql_risk("SELECT * FROM users") == SQLRiskLevel.SAFE

    def test_insert_is_safe(self):
        assert classify_sql_risk("INSERT INTO users (name) VALUES ('test')") == SQLRiskLevel.SAFE

    def test_update_is_safe(self):
        assert classify_sql_risk("UPDATE users SET name = 'test' WHERE id = 1") == SQLRiskLevel.SAFE

    def test_bounded_delete_is_safe(self):
        assert classify_sql_risk("DELETE FROM users WHERE id = 'x'") == SQLRiskLevel.SAFE

    def test_unbounded_delete_is_destructive(self):
        assert classify_sql_risk("DELETE FROM users") == SQLRiskLevel.DESTRUCTIVE

    def test_parenthesized_tautological_delete_is_destructive(self):
        assert classify_sql_risk("DELETE FROM users WHERE (1=1)") == SQLRiskLevel.DESTRUCTIVE
        assert classify_sql_risk("DELETE FROM users WHERE ((true))") == SQLRiskLevel.DESTRUCTIVE

    def test_commented_tautological_delete_is_destructive(self):
        assert classify_sql_risk("DELETE FROM users WHERE 1=1 /* comment */") == SQLRiskLevel.DESTRUCTIVE
        assert classify_sql_risk("DELETE FROM users WHERE true -- comment") == SQLRiskLevel.DESTRUCTIVE

    def test_or_tautological_delete_is_destructive(self):
        assert classify_sql_risk("DELETE FROM users WHERE id = 1 OR 1=1") == SQLRiskLevel.DESTRUCTIVE
        assert classify_sql_risk("DELETE FROM users WHERE 1=1 OR id = 1") == SQLRiskLevel.DESTRUCTIVE
        assert classify_sql_risk("DELETE FROM users WHERE (id = 1 OR true)") == SQLRiskLevel.DESTRUCTIVE
        assert classify_sql_risk("DELETE FROM users WHERE id = 1 OR (1=1)") == SQLRiskLevel.DESTRUCTIVE
        assert classify_sql_risk("DELETE FROM users WHERE id = 1 OR ((true))") == SQLRiskLevel.DESTRUCTIVE

    def test_self_comparison_delete_is_destructive(self):
        assert classify_sql_risk("DELETE FROM users WHERE id = id") == SQLRiskLevel.DESTRUCTIVE
        assert classify_sql_risk("DELETE FROM users WHERE `id` = `id`") == SQLRiskLevel.DESTRUCTIVE
        assert classify_sql_risk("DELETE FROM users WHERE users.id = users.id") == SQLRiskLevel.DESTRUCTIVE
        assert classify_sql_risk("DELETE FROM users WHERE id = id OR tenant_id = 1") == SQLRiskLevel.DESTRUCTIVE

    def test_null_safe_self_comparison_delete_is_destructive(self):
        assert classify_sql_risk("DELETE FROM users WHERE id <=> id") == SQLRiskLevel.DESTRUCTIVE
        assert classify_sql_risk("DELETE FROM users WHERE `id` <=> `id`") == SQLRiskLevel.DESTRUCTIVE
        assert classify_sql_risk("DELETE FROM users WHERE users.id <=> users.id") == SQLRiskLevel.DESTRUCTIVE
        assert classify_sql_risk("DELETE FROM users WHERE id = 1 OR id <=> id") == SQLRiskLevel.DESTRUCTIVE

    def test_non_self_comparison_delete_stays_safe(self):
        assert classify_sql_risk("DELETE FROM users WHERE id = other_id") == SQLRiskLevel.SAFE
        assert classify_sql_risk("DELETE FROM users WHERE id <=> other_id") == SQLRiskLevel.SAFE
        assert classify_sql_risk("DELETE FROM users WHERE name = 'name'") == SQLRiskLevel.SAFE

    def test_or_tautology_inside_string_literal_is_not_destructive(self):
        assert classify_sql_risk("DELETE FROM users WHERE name = 'x OR 1=1'") == SQLRiskLevel.SAFE

    def test_drop_table_is_destructive(self):
        assert classify_sql_risk("DROP TABLE users") == SQLRiskLevel.DESTRUCTIVE

    def test_truncate_is_destructive(self):
        assert classify_sql_risk("TRUNCATE TABLE users") == SQLRiskLevel.DESTRUCTIVE

    def test_alter_add_column_is_destructive(self):
        assert classify_sql_risk("ALTER TABLE users ADD COLUMN x INT") == SQLRiskLevel.DESTRUCTIVE

    def test_alter_drop_column_is_destructive(self):
        assert classify_sql_risk("ALTER TABLE users DROP COLUMN x") == SQLRiskLevel.DESTRUCTIVE

    def test_drop_database_is_blocked(self):
        assert classify_sql_risk("DROP DATABASE mydb") == SQLRiskLevel.BLOCKED

    def test_drop_database_case_insensitive(self):
        assert classify_sql_risk("drop database mydb") == SQLRiskLevel.BLOCKED

    def test_executable_comment_drop_table_is_destructive(self):
        assert classify_sql_risk("/*!50000 DROP TABLE users */") == SQLRiskLevel.DESTRUCTIVE

    def test_executable_comment_drop_database_is_blocked(self):
        assert classify_sql_risk("/*!50000 DROP DATABASE mydb */") == SQLRiskLevel.BLOCKED

    def test_executable_comment_unbounded_delete_is_destructive(self):
        assert classify_sql_risk("/*!50000 DELETE FROM users */") == SQLRiskLevel.DESTRUCTIVE

    def test_leading_comment_statement_drop_table_is_destructive(self):
        assert classify_sql_risk("/* comment */; DROP TABLE users") == SQLRiskLevel.DESTRUCTIVE

    def test_leading_comment_statement_drop_database_is_blocked(self):
        assert classify_sql_risk("/* comment */; DROP DATABASE mydb") == SQLRiskLevel.BLOCKED

    def test_leading_comment_statement_unbounded_delete_is_destructive(self):
        assert classify_sql_risk("/* comment */; DELETE FROM users") == SQLRiskLevel.DESTRUCTIVE

    def test_show_is_safe(self):
        assert classify_sql_risk("SHOW TABLES") == SQLRiskLevel.SAFE

    def test_describe_is_safe(self):
        assert classify_sql_risk("DESCRIBE users") == SQLRiskLevel.SAFE

    def test_explain_is_safe(self):
        assert classify_sql_risk("EXPLAIN SELECT * FROM users") == SQLRiskLevel.SAFE


class TestIsReadonlySql:
    """Test is_readonly_sql — used to enforce READONLY permission at the
    application level.  Read-only SQL is allowed; write SQL is rejected."""

    def test_select_is_readonly(self):
        assert is_readonly_sql("SELECT * FROM users")

    def test_select_with_subquery(self):
        assert is_readonly_sql("SELECT id FROM users WHERE id > 10")

    def test_show_tables(self):
        assert is_readonly_sql("SHOW TABLES")

    def test_show_variables(self):
        assert is_readonly_sql("SHOW VARIABLES LIKE 'read_only'")

    def test_describe(self):
        assert is_readonly_sql("DESCRIBE users")

    def test_desc(self):
        assert is_readonly_sql("DESC users")

    def test_explain(self):
        assert is_readonly_sql("EXPLAIN SELECT * FROM users")

    def test_insert_not_readonly(self):
        assert not is_readonly_sql("INSERT INTO users (name) VALUES ('test')")

    def test_update_not_readonly(self):
        assert not is_readonly_sql("UPDATE users SET name = 'test' WHERE id = 1")

    def test_delete_not_readonly(self):
        assert not is_readonly_sql("DELETE FROM users WHERE id = 1")

    def test_create_not_readonly(self):
        assert not is_readonly_sql("CREATE TABLE t (id INT)")

    def test_alter_not_readonly(self):
        assert not is_readonly_sql("ALTER TABLE users ADD COLUMN x INT")

    def test_drop_not_readonly(self):
        assert not is_readonly_sql("DROP TABLE users")

    def test_case_insensitive_select(self):
        assert is_readonly_sql("select * from users")

    def test_case_insensitive_show(self):
        assert is_readonly_sql("show tables")

    def test_comment_prefixed_show_is_readonly(self):
        assert is_readonly_sql("/* comment */ SHOW TABLES")
        assert is_readonly_sql("-- comment\nSHOW TABLES")
        assert is_readonly_sql("# comment\nSHOW TABLES")

    def test_comment_statement_then_readonly_sql_is_readonly(self):
        assert is_readonly_sql("/* comment */; SELECT 1")
        assert is_readonly_sql("/* comment */; SHOW TABLES")

    def test_comment_prefixed_describe_is_readonly(self):
        assert is_readonly_sql("/* comment */ DESCRIBE users")
        assert is_readonly_sql("-- comment\nDESC users")

    def test_comment_prefixed_explain_is_readonly(self):
        assert is_readonly_sql("/* comment */ EXPLAIN SELECT * FROM users")

    def test_executable_comment_select_is_readonly(self):
        assert is_readonly_sql("/*!50000 SELECT * FROM users */")

    def test_executable_comment_show_is_readonly(self):
        assert is_readonly_sql("/*!50000 SHOW TABLES */")

    def test_executable_comment_drop_not_readonly(self):
        assert not is_readonly_sql("/*!50000 DROP TABLE users */")

    def test_multiple_statements_not_readonly(self):
        assert not is_readonly_sql("SELECT 1; INSERT INTO t VALUES (1)")
        assert not is_readonly_sql("SHOW TABLES; USE db1")
