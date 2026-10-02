import asyncio
import json
import os
import sqlite3
import unittest
from unittest.mock import patch

from muse.database_context import (
    DatabaseContextError, _read_sqlite_catalog, _reflect_schema,
    read_database_schema, validate_database_connections,
)
from muse.repository import RepositoryCoordinator
from muse.repository_context import RepositoryIndexer


SERVER = {"name": "server", "type": "postgresql", "host": "localhost", "database": "employees",
          "username": "schema_reader", "password_env": "APP_DB_PASSWORD"}
TURSO = {"name": "turso", "type": "turso", "url": "libsql://example.turso.io", "token_env": "APP_TURSO_TOKEN"}


class DatabaseContextTests(unittest.TestCase):
    def test_both_sources_and_validation(self):
        self.assertEqual(len(validate_database_connections([SERVER, TURSO])), 2)
        for item in ({**SERVER, "password": "secret"}, {**SERVER, "port": True},
                     {**TURSO, "url": "https://token@example.com"}, {**TURSO, "url": "file:/tmp/db"}):
            with self.subTest(item=item), self.assertRaises(DatabaseContextError):
                validate_database_connections([item])
        with self.assertRaises(DatabaseContextError):
            validate_database_connections([SERVER, SERVER])

    def test_turso_catalog_reads_only_metadata_and_handles_quoted_names(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.executescript('''
            CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT);
            CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id));
            CREATE INDEX order_user ON orders(user_id);
            CREATE TABLE "odd\"\"table" ("key" TEXT);
            CREATE VIEW user_view AS SELECT id FROM users;
            INSERT INTO users VALUES (1, 'private-user-value');
        ''')
        statements = []
        connection.set_trace_callback(statements.append)
        tables, truncated = _read_sqlite_catalog(connection)
        self.assertFalse(truncated)
        names = {table["name"]: table for table in tables}
        self.assertIn('odd"table', names)
        self.assertEqual(names["orders"]["foreign_keys"][0]["target_table"], "users")
        self.assertEqual(names["orders"]["indexes"][0]["columns"], ["user_id"])
        self.assertNotIn("private-user-value", json.dumps(tables))
        self.assertTrue(all(sql.startswith("PRAGMA") or "sqlite_master" in sql for sql in statements))

    def test_sqlalchemy_reflection_with_real_local_database(self):
        from sqlalchemy import create_engine, inspect

        engine = create_engine("sqlite:///:memory:")
        self.addCleanup(engine.dispose)
        with engine.connect() as connection:
            connection.exec_driver_sql("CREATE TABLE users (id INTEGER PRIMARY KEY)")
            connection.exec_driver_sql("CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id))")
            tables, truncated = _reflect_schema(inspect(connection), None)
        self.assertFalse(truncated)
        order = next(table for table in tables if table["name"] == "orders")
        self.assertEqual(order["foreign_keys"][0]["target_table"], "users")
        self.assertEqual(order["primary_key"], ["id"])

    def test_server_driver_selection_and_password_encoding(self):
        from sqlalchemy import create_engine

        for kind, driver in (("postgresql", "postgresql+psycopg"), ("mysql", "mysql+pymysql"),
                             ("sqlserver", "mssql+pymssql")):
            with self.subTest(kind=kind):
                engine = create_engine("sqlite:///:memory:")
                with patch.dict(os.environ, {"APP_DB_PASSWORD": "p@ss:word/with?chars"}), patch(
                    "sqlalchemy.create_engine", return_value=engine
                ) as factory:
                    metadata = read_database_schema({**SERVER, "type": kind})
                url = factory.call_args.args[0]
                self.assertEqual(url.drivername, driver)
                self.assertEqual(url.password, "p@ss:word/with?chars")
                self.assertNotIn(url.password, json.dumps(metadata))

    def test_both_database_sources_are_merged_before_agents_run(self):
        def schema(config):
            return {"name": config["name"], "type": config["type"], "tables": [], "truncated": False}

        with patch("muse.repository.read_database_schema", side_effect=schema) as reader:
            coordinator = RepositoryCoordinator(None, database_connections=[SERVER, TURSO])
            asyncio.run(coordinator.prepare())
        self.assertEqual(reader.call_count, 2)
        self.assertEqual([database["type"] for database in coordinator.context_package().databases],
                         ["postgresql", "turso"])

    def test_errors_hide_driver_credentials(self):
        with patch.dict(os.environ, {"APP_DB_PASSWORD": "secret-password"}), patch(
            "muse.database_context._read_server", side_effect=RuntimeError("secret-password user@localhost")
        ):
            with self.assertRaises(DatabaseContextError) as error:
                read_database_schema(SERVER)
        self.assertNotIn("secret-password", str(error.exception))
        self.assertNotIn("user@localhost", str(error.exception))

    def test_database_only_context_and_orm_merge(self):
        database = {"name": "server", "type": "postgresql", "truncated": False, "tables": [
            {"name": "users", "schema": "public", "kind": "table",
             "columns": [{"name": "id", "type": "INTEGER", "nullable": False}],
             "primary_key": ["id"], "foreign_keys": [], "indexes": []},
        ]}
        with patch("muse.repository.read_database_schema", return_value=database):
            coordinator = RepositoryCoordinator(None, database_connections=[SERVER])
            prompt = asyncio.run(coordinator.prepare())
        self.assertIn("public.users", prompt)
        self.assertEqual(coordinator.tools(), [])
        self.assertIn("database-schema.json", coordinator.context_package().artifacts())
        context = RepositoryIndexer().index(
            [("models.py", "class User(Base):\n    __tablename__ = 'users'\n")], repository="example/project"
        ).with_databases([database])
        self.assertEqual(context.relationships[0].target_path, "database://server/public.users")
        # Matching a bare name in two databases must not silently choose one.
        ambiguous = RepositoryIndexer().index(
            [("models.py", "class User(Base):\n    __tablename__ = 'users'\n")]
        ).with_databases([database, {**database, "name": "other"}])
        self.assertEqual(ambiguous.relationships[0].target_path, "")


if __name__ == "__main__":
    unittest.main()
