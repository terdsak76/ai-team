import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from github_repository import GitHubRepositoryError
from muse.orchestrator import MuseOrchestrator
from muse.repository import DATABASE_CONTEXT_TTL_SECONDS
from muse.repository_context import RepositoryContext
from turso_store import TursoStore
from workflow import refresh_repo_context


REVISION = "a" * 40
CONNECTIONS = [{"name": "app", "type": "turso", "url": "libsql://example.turso.io", "token_env": "APP_TOKEN"}]
SCHEMA = {"name": "app", "type": "turso", "tables": [], "truncated": False}


class RepositoryContextCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = TursoStore(url="test", token="test")
        self.store._connect = lambda: sqlite3.connect(str(Path(self.directory.name) / "store.db"))
        self.store_patch = patch("turso_store.TursoStore", return_value=self.store)
        self.store_patch.start()
        self.addCleanup(self.store_patch.stop)
        self.runner = AsyncMock(return_value=SimpleNamespace(final_output="Observed models.py User maps to users."))
        self.runner_patch = patch("muse.orchestrator.Runner.run", self.runner)
        self.runner_patch.start()
        self.addCleanup(self.runner_patch.stop)
        self.schema_reader = Mock(return_value=SCHEMA)
        self.schema_patch = patch("muse.repository.read_database_schema", self.schema_reader)
        self.schema_patch.start()
        self.addCleanup(self.schema_patch.stop)

    def orchestrator(self, project_id=1, revision=REVISION, connections=None, repository=True):
        instance = MuseOrchestrator(project_id=project_id,
                                    repository_url="https://github.com/example/project" if repository else None,
                                    database_connections=connections)
        if repository:
            remote = instance.repository._repository
            remote.resolve_revision = Mock(return_value=revision)
            remote.indexable_file_paths = Mock(return_value=["models.py", "schema.sql"])
            remote.read_file = Mock(side_effect=lambda path: {
                "models.py": "class User(Base):\n    __tablename__ = 'users'\n",
                "schema.sql": "CREATE TABLE users (id INTEGER PRIMARY KEY);",
            }[path])
        return instance

    async def test_second_requirement_reuses_snapshot_without_indexing_schema_or_llm_stage(self):
        first = await self.orchestrator(connections=CONNECTIONS).prepare_repo_context()
        self.runner.reset_mock()
        self.schema_reader.reset_mock()
        second = self.orchestrator(connections=CONNECTIONS)
        events = []
        result = await second.run("Another requirement", on_event=events.append)
        self.assertTrue(result.repo_context["cache"]["reused"])
        self.assertEqual(result.repository_snapshot, first["snapshot"])
        second.repository._repository.resolve_revision.assert_called_once()
        second.repository._repository.read_file.assert_not_called()
        second.repository._repository.indexable_file_paths.assert_not_called()
        self.schema_reader.assert_not_called()
        self.assertEqual(self.runner.await_count, 5)
        for call in self.runner.call_args_list:
            self.assertIn(first["summary"], call.args[0].instructions)
            self.assertTrue(call.args[0].tools)  # Targeted reads remain available.
        self.assertEqual([e["status"] for e in events if e["type"] == "repo_context"], ["checking", "reused"])
        self.assertEqual(self.store._with_connection(lambda conn: conn.execute(
            "SELECT COUNT(*) FROM repo_map_snapshot").fetchone()[0]), 1)

    async def test_commit_change_rebuilds_and_preserves_previous_snapshot(self):
        first = await self.orchestrator().prepare_repo_context()
        second = await self.orchestrator(revision="b" * 40).prepare_repo_context()
        self.assertFalse(second["cache"]["reused"])
        self.assertEqual(second["cache"]["reason"], "repository_changed")
        self.assertEqual(second["revision"], "b" * 40)
        self.assertNotEqual(first["snapshot"]["snapshot_id"], second["snapshot"]["snapshot_id"])
        old = self.store.get_repository_snapshot(first["snapshot"]["snapshot_id"])
        self.assertEqual(old["is_current"], 0)
        self.assertEqual(old["revision"], REVISION)
        self.assertTrue(old["artifacts"])

    async def test_connection_change_invalidates_cache(self):
        await self.orchestrator(connections=CONNECTIONS).prepare_repo_context()
        changed = [dict(CONNECTIONS[0], url="libsql://other.turso.io")]
        result = await self.orchestrator(connections=changed).prepare_repo_context()
        self.assertEqual(result["cache"]["reason"], "settings_changed")
        self.assertFalse(result["cache"]["reused"])
        self.assertEqual(self.schema_reader.call_count, 2)

    async def test_database_ttl_is_based_on_creation_and_not_extended_on_reuse(self):
        first = await self.orchestrator(connections=CONNECTIONS).prepare_repo_context()
        reused = await self.orchestrator(connections=CONNECTIONS).prepare_repo_context()
        self.assertEqual(first["cache"]["expires_at"], reused["cache"]["expires_at"])
        stale = (datetime.now(timezone.utc) - timedelta(seconds=DATABASE_CONTEXT_TTL_SECONDS + 1)).isoformat()
        self.store._with_connection(lambda conn: conn.execute(
            "UPDATE repo_map_snapshot SET created_at = ? WHERE id = ?",
            (stale, first["snapshot"]["snapshot_id"])))
        rebuilt = await self.orchestrator(connections=CONNECTIONS).prepare_repo_context()
        self.assertEqual(rebuilt["cache"]["reason"], "database_schema_expired")
        self.assertEqual(self.schema_reader.call_count, 2)

    async def test_database_only_cache_reuses_and_expires(self):
        first = await self.orchestrator(connections=CONNECTIONS, repository=False).prepare_repo_context()
        second = await self.orchestrator(connections=CONNECTIONS, repository=False).prepare_repo_context()
        self.assertTrue(second["cache"]["reused"])
        self.assertEqual(self.schema_reader.call_count, 1)
        self.assertEqual(first["snapshot"], second["snapshot"])
        self.assertEqual(self.runner.await_count, 1)

    async def test_reordering_database_sources_does_not_rebuild(self):
        connections = [CONNECTIONS[0], dict(CONNECTIONS[0], name="other")]
        with patch("muse.repository.read_database_schema", side_effect=lambda config: dict(SCHEMA, name=config["name"])):
            first = await self.orchestrator(connections=connections, repository=False).prepare_repo_context()
            second = await self.orchestrator(connections=list(reversed(connections)), repository=False).prepare_repo_context()
        self.assertTrue(second["cache"]["reused"])
        self.assertEqual(first["snapshot"], second["snapshot"])

    async def test_repository_only_cache_has_no_schema_expiry(self):
        first = await self.orchestrator().prepare_repo_context()
        stale = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        self.store._with_connection(lambda conn: conn.execute(
            "UPDATE repo_map_snapshot SET created_at = ? WHERE id = ?",
            (stale, first["snapshot"]["snapshot_id"])))
        reused = await self.orchestrator().prepare_repo_context()
        self.assertTrue(reused["cache"]["reused"])
        self.assertIsNone(reused["cache"]["expires_at"])

    async def test_project_and_workspace_caches_are_isolated(self):
        one = await self.orchestrator(project_id=1).prepare_repo_context()
        two = await self.orchestrator(project_id=2).prepare_repo_context()
        workspace = await self.orchestrator(project_id=None).prepare_repo_context()
        self.assertFalse(two["cache"]["reused"])
        self.assertFalse(workspace["cache"]["reused"])
        reused = await self.orchestrator(project_id=1).prepare_repo_context()
        self.assertEqual(reused["snapshot"]["snapshot_id"], one["snapshot"]["snapshot_id"])

    async def test_force_refresh_rebuilds_even_without_changes(self):
        first = await self.orchestrator().prepare_repo_context()
        second = await self.orchestrator().prepare_repo_context(force_refresh=True)
        self.assertEqual(second["cache"]["reason"], "manual_refresh")
        self.assertNotEqual(first["snapshot"]["snapshot_id"], second["snapshot"]["snapshot_id"])
        self.assertEqual(self.runner.await_count, 2)

    async def test_legacy_and_corrupt_snapshots_rebuild(self):
        for content in ["not json", json.dumps(RepositoryContext(repository="example/project", revision="main").as_dict())]:
            with self.subTest(content=content):
                first = await self.orchestrator().prepare_repo_context(force_refresh=True)
                self.store._with_connection(lambda conn: conn.execute(
                    "UPDATE repo_map_snapshot SET context_json = ? WHERE id = ?", (content, first["snapshot"]["snapshot_id"])))
                result = await self.orchestrator().prepare_repo_context()
                self.assertFalse(result["cache"]["reused"])

    async def test_github_access_failure_is_not_hidden_by_existing_cache(self):
        await self.orchestrator().prepare_repo_context()
        denied = self.orchestrator()
        denied.repository._repository.resolve_revision.side_effect = GitHubRepositoryError("Access denied")
        self.runner.reset_mock()
        with self.assertRaisesRegex(GitHubRepositoryError, "Access denied"):
            await denied.prepare_repo_context()
        self.runner.assert_not_called()

    async def test_changed_analysis_policy_invalidates_cache(self):
        await self.orchestrator().prepare_repo_context()
        from team_agents.repo_context import repo_context_agent

        with patch.object(repo_context_agent, "instructions", repo_context_agent.instructions + "\nNew policy."):
            changed = await self.orchestrator().prepare_repo_context()
        self.assertEqual(changed["cache"]["reason"], "settings_changed")

    async def test_failed_manual_refresh_does_not_replace_good_cache(self):
        first = await self.orchestrator().prepare_repo_context()
        self.runner.return_value = SimpleNamespace(final_output=" ")
        with self.assertRaises(ValueError):
            await self.orchestrator().prepare_repo_context(force_refresh=True)
        reused = await self.orchestrator().prepare_repo_context()
        self.assertEqual(reused["snapshot"], first["snapshot"])
        self.assertTrue(reused["cache"]["reused"])

    async def test_single_agent_rerun_reuses_the_project_cache(self):
        first = await self.orchestrator().prepare_repo_context()
        run = self.store.save_run(requirement_code="REQ-1", project_name="test", request_text="task",
                                  outputs={"backend": "initial"})
        self.runner.reset_mock()
        with patch("muse.orchestrator.TursoStore", return_value=self.store):
            result = await self.orchestrator().run_single_agent(agent_key="backend", run_id=run["run_id"], specification="spec")
        self.assertEqual(self.runner.await_count, 1)
        self.assertTrue(result["repo_context"]["cache"]["reused"])
        self.assertEqual(result["repo_context"]["snapshot"], first["snapshot"])

    async def test_refresh_workflow_uses_saved_project_credentials_and_runs_only_context(self):
        project = {"github_repo": "https://github.com/example/private", "github_token": "private-token",
                   "database_connections": CONNECTIONS}
        with patch.object(self.store, "get_project", return_value=project), \
             patch("workflow.MuseOrchestrator") as orchestrator:
            orchestrator.return_value.prepare_repo_context = AsyncMock(return_value={"cache": {"reused": False}})
            response = await refresh_repo_context(project_id=7)
        self.assertEqual(orchestrator.call_args.kwargs["github_token"], "private-token")
        self.assertEqual(orchestrator.call_args.kwargs["database_connections"], CONNECTIONS)
        orchestrator.return_value.prepare_repo_context.assert_awaited_once_with(None, force_refresh=True)
        orchestrator.return_value.run.assert_not_called()
        self.assertFalse(response["repo_context"]["cache"]["reused"])


if __name__ == "__main__":
    unittest.main()
