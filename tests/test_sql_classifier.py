# tests/test_sql_classifier.py
import pytest
from server.core.sql_classifier import classify_sql


class TestClassifySql:
    @pytest.mark.parametrize("sql,expected", [
        ("SELECT * FROM users", "SELECT"),
        ("select 1", "SELECT"),
        ("INSERT INTO t VALUES (1)", "INSERT"),
        ("UPDATE t SET x=1", "UPDATE"),
        ("DELETE FROM t", "DELETE"),
        ("CREATE TABLE t (id INT)", "CREATE"),
        ("ALTER TABLE t ADD col INT", "ALTER"),
        ("DROP TABLE t", "DROP"),
        ("TRUNCATE TABLE t", "TRUNCATE"),
        ("SHOW TABLES", "SHOW"),
        ("DESCRIBE users", "DESCRIBE"),
        ("EXPLAIN SELECT 1", "EXPLAIN"),
        ("USE mydb", "USE"),
    ])
    def test_known_types(self, sql, expected):
        assert classify_sql(sql) == expected

    @pytest.mark.parametrize("sql,expected", [
        ("-- reason\nSELECT * FROM t", "SELECT"),
        ("/* app */ UPDATE t SET x=1", "UPDATE"),
        ("-- line1\n-- line2\nDELETE FROM t", "DELETE"),
        ("/* multi\nline\ncomment */ INSERT INTO t VALUES(1)", "INSERT"),
    ])
    def test_comment_prefixed(self, sql, expected):
        assert classify_sql(sql) == expected

    @pytest.mark.parametrize("sql,expected", [
        (None, "OTHER"),
        ("", "OTHER"),
        ("   ", "OTHER"),
        ("-- only comment\n", "OTHER"),
        ("/* only block comment */", "OTHER"),
        ("CALL my_proc()", "OTHER"),
        ("SET @var = 1", "OTHER"),
    ])
    def test_edge_cases(self, sql, expected):
        assert classify_sql(sql) == expected
