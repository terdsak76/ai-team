import unittest
import sqlite3
import tempfile
from pathlib import Path

from muse.repository_context import RepositoryIndexer
from turso_store import TursoStore


class _Cursor:
    def __init__(self, lastrowid=None):
        self.lastrowid = lastrowid
        self.description = None

    def fetchall(self):
        return []


class _Connection:
    def __init__(self):
        self.statements = []
        self.committed = False
        self.closed = False

    def execute(self, sql, params=()):
        self.statements.append((sql, params))
        if "INSERT INTO repo_map_snapshot" in sql:
            return _Cursor(lastrowid=42)
        return _Cursor()

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


class TursoRepositoryContextTests(unittest.TestCase):
    def test_project_connections_round_trip_and_snapshot_schema_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TursoStore(url="test", token="test")
            store._connect = lambda: sqlite3.connect(str(Path(directory) / "store.db"))
            connections = [{"name": "turso", "type": "turso", "url": "libsql://example.turso.io", "token_env": "APP_TURSO_TOKEN"}]
            prompts = {key: "test" for key in store.PROJECT_PROMPT_KEYS}
            project = store.create_project(project_name="Database project", system_prompts=prompts,
                                           database_connections=connections)
            self.assertEqual(store.list_projects()[0]["database_connections"], connections)
            project = store.update_project(project_id=project["id"], project_name="Database project", system_prompts=prompts)
            self.assertEqual(project["database_connections"], connections)
            context = RepositoryIndexer().index([], repository="database:turso").with_databases([
                {"name": "turso", "type": "turso", "tables": [], "truncated": False}
            ])
            saved = store.save_repository_context(context=context, project_id=project["id"])
            snapshot = store.get_repository_snapshot(saved["snapshot_id"])
            self.assertIn("database-schema.json", {artifact["artifact_path"] for artifact in snapshot["artifacts"]})
            project = store.update_project(project_id=project["id"], project_name="Database project", system_prompts=prompts,
                                           database_connections=[])
            self.assertEqual(project["database_connections"], [])

    def test_saves_artifacts_and_relationship_edges(self):
        context = RepositoryIndexer().index(
            [
                ("db/schema.sql", "CREATE TABLE users (id INTEGER PRIMARY KEY);"),
                (
                    "app/models.py",
                    "class User(Base):\n    __tablename__ = 'users'\n",
                ),
            ],
            repository="example/orders",
            revision="main",
        )
        connection = _Connection()
        store = TursoStore(url="file:test", token="test")
        store._connect = lambda: connection
        store._ensure_schema = lambda conn: None

        result = store.save_repository_context(context=context, project_id=7)

        self.assertEqual(result["snapshot_id"], 42)
        self.assertGreater(result["artifact_count"], 2)
        self.assertTrue(connection.committed)
        self.assertTrue(connection.closed)
        sql = "\n".join(statement for statement, _ in connection.statements)
        self.assertIn("INSERT INTO repo_map_snapshot", sql)
        self.assertIn("INSERT INTO repo_map_artifact", sql)
        self.assertIn("INSERT INTO repo_relationship", sql)


if __name__ == "__main__":
    unittest.main()
