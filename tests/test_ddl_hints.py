from server.core.ddl_hints import DDL_COMMENT_HINT, should_add_comment_hint


class TestShouldAddCommentHint:
    def test_create_table_without_comment(self):
        sql = "CREATE TABLE users (id INT PRIMARY KEY, name VARCHAR(100))"
        assert should_add_comment_hint(sql) is True

    def test_create_table_with_table_comment(self):
        sql = "CREATE TABLE users (id INT) COMMENT 'user table'"
        assert should_add_comment_hint(sql) is False

    def test_create_table_with_column_comment(self):
        sql = "CREATE TABLE users (id INT COMMENT 'primary key', name VARCHAR(100))"
        assert should_add_comment_hint(sql) is False

    def test_executable_comment_create_table_without_comment(self):
        sql = "/*!50000 CREATE TABLE users (id INT PRIMARY KEY, name VARCHAR(100)) */"
        assert should_add_comment_hint(sql) is True

    def test_executable_comment_create_table_with_comment(self):
        sql = "/*!50000 CREATE TABLE users (id INT) COMMENT 'user table' */"
        assert should_add_comment_hint(sql) is False

    def test_alter_table_add_column(self):
        sql = "ALTER TABLE users ADD COLUMN email VARCHAR(255)"
        assert should_add_comment_hint(sql) is True

    def test_alter_table_modify_column(self):
        sql = "ALTER TABLE users MODIFY COLUMN name VARCHAR(200)"
        assert should_add_comment_hint(sql) is True

    def test_select_statement(self):
        sql = "SELECT * FROM users"
        assert should_add_comment_hint(sql) is False

    def test_insert_statement(self):
        sql = "INSERT INTO users (name) VALUES ('test')"
        assert should_add_comment_hint(sql) is False

    def test_string_literal_containing_create_table(self):
        sql = "SELECT * FROM logs WHERE message = 'CREATE TABLE test (id INT)'"
        assert should_add_comment_hint(sql) is False

    def test_alter_table_with_comment(self):
        sql = "ALTER TABLE users ADD COLUMN email VARCHAR(255) COMMENT 'email address'"
        assert should_add_comment_hint(sql) is False

    def test_hint_text_is_under_200_chars(self):
        assert len(DDL_COMMENT_HINT) <= 200
