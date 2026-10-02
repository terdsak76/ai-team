import unittest
from unittest.mock import AsyncMock, patch

from muse.orchestrator import MuseRunResult, run_project
from muse.models import ConflictForecast
from workflow import run_single_agent


CONNECTIONS = [{"name": "turso", "type": "turso", "url": "libsql://example.turso.io", "token_env": "APP_TURSO_TOKEN"}]
PROJECT = {"github_repo": "https://github.com/example/project", "github_token": "saved-token",
           "system_prompts": {"backend": "saved prompt"}, "database_connections": CONNECTIONS,
           "project_context": "existing context"}


class DatabaseWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_run_uses_saved_database_and_repository_settings(self):
        with patch("muse.orchestrator.TursoStore") as store, patch("muse.orchestrator.MuseOrchestrator") as orchestrator:
            store.return_value.get_project.return_value = PROJECT
            store.return_value.get_relevant_project_memories.return_value = []
            store.return_value.save_run.return_value = {"run_id": "test-run"}
            orchestrator.return_value.run = AsyncMock(return_value=MuseRunResult(
                "spec", "ui", "frontend", "backend", "tests", ConflictForecast(),
            ))
            await run_project("task", project_id=1)
            self.assertEqual(orchestrator.call_args.kwargs["database_connections"], CONNECTIONS)
            self.assertEqual(orchestrator.call_args.args[1], PROJECT["github_repo"])

    async def test_single_run_can_disable_saved_database_sources(self):
        with patch("turso_store.TursoStore") as store, patch("workflow.MuseOrchestrator") as orchestrator:
            store.return_value.get_project.return_value = PROJECT
            orchestrator.return_value.run_single_agent = AsyncMock(return_value={"output": "test"})
            await run_single_agent(agent_key="backend", run_id="test-run", specification="spec",
                                   project_id=1, database_connections=[])
            self.assertEqual(orchestrator.call_args.kwargs["database_connections"], [])
            self.assertEqual(orchestrator.call_args.args[1], PROJECT["github_repo"])


if __name__ == "__main__":
    unittest.main()
