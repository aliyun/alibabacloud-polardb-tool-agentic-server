"""Backend DDL must not disable a later SQLite foreign-key assertion."""
import pytest
from sqlalchemy import create_mock_engine
from sqlalchemy.dialects import sqlite
from sqlalchemy.schema import CreateTable

from server.models import User
from tests._helpers import isolated_model_metadata


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_external_backend_ddl_preserves_source_model_constraints(dialect):
    metadata = isolated_model_metadata()
    statements = []
    engine = create_mock_engine(f"{dialect}://", lambda statement, *_a, **_kw: statements.append(statement))
    metadata.create_all(engine, checkfirst=False)
    assert any("ALTER TABLE users ADD CONSTRAINT fk_users_default_instance_id" in str(statement.compile(dialect=engine.dialect))
               for statement in statements)
    # SQLAlchemy AddConstraint isolates the copied constraint from CREATE TABLE.
    # The source table must still emit its released SET NULL foreign key on SQLite.
    ddl = str(CreateTable(User.__table__).compile(dialect=sqlite.dialect()))
    assert "CONSTRAINT fk_users_default_instance_id" in ddl
    assert "REFERENCES instances (id) ON DELETE SET NULL" in ddl
