import asyncio
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from api._progress import ProgressStreamMixin
from api._handler import ApiHandler
from api.repo_context import handler as RepoContextHandler, load_snapshot
from muse.orchestrator import MuseOrchestrator, run_project
from muse.repository_context import RepositoryContext, RepositoryIndexer
from turso_store import TursoStore


class RepoContextAgentTests(unittest.IsolatedAsyncioTestCase):
    def coordinator(self, orchestrator, context):
        # Use the real coordinator's instructions, summary update and artifacts;
        # replace only its external repository/schema reads and Turso write.
        orchestrator.repository._context_package = context
        orchestrator.repository.prepare = AsyncMock(return_value=orchestrator.repository.instructions())
        orchestrator.repository.persist_context = lambda **kwargs: {
            "snapshot_id": 42, "repository": context.repository,
        }
        return orchestrator

    async def test_summary_precedes_development_and_is_shared_and_persisted(self):
        context = RepositoryIndexer().index([
            ("models.py", "class User(Base):\n    __tablename__ = 'users'\n"),
            ("schema.sql", "CREATE TABLE users (id INTEGER PRIMARY KEY);"),
        ], repository="example/repo")
        orchestrator = self.coordinator(MuseOrchestrator(), context)
        calls = []
        events = []
        persisted = []

        def persist(**kwargs):
            persisted.append(orchestrator.repository.context_package())
            return {"snapshot_id": 42}

        orchestrator.repository.persist_context = persist

        async def runner(agent, prompt):
            calls.append(agent)
            return SimpleNamespace(final_output="Observed users mapping in models.py." if len(calls) == 1 else "output")

        with patch("muse.orchestrator.Runner.run", side_effect=runner):
            result = await orchestrator.run("new task", on_event=events.append)
        self.assertEqual(calls[0].name, "Repo Context Architect")
        self.assertEqual(len(calls), 6)
        for agent in calls[1:]:
            self.assertIn("Observed users mapping in models.py.", agent.instructions)
        self.assertEqual(len(persisted), 1)
        self.assertIn("Observed users mapping", persisted[0].artifacts()["architecture.md"])
        self.assertEqual(result.repo_context["file_count"], 2)
        self.assertGreater(result.repo_context["relationship_count"], 0)
        self.assertEqual(result.repository_snapshot["snapshot_id"], 42)
        context_states = [event["status"] for event in events if event["type"] == "repo_context"]
        self.assertEqual(context_states, ["checking", "indexing", "summarizing", "saving", "ready"])
        self.assertLess(next(i for i, e in enumerate(events) if e.get("status") == "ready"),
                        next(i for i, e in enumerate(events) if e["type"] == "agent"))

    async def test_unconfigured_sources_skip_llm_and_persistence(self):
        orchestrator = MuseOrchestrator()
        with patch("muse.orchestrator.Runner.run", new_callable=AsyncMock) as runner, \
             patch.object(orchestrator.repository, "persist_context") as persist:
            report = await orchestrator.prepare_repo_context()
        runner.assert_not_called()
        persist.assert_not_called()
        self.assertEqual(report["status"], "skipped")
        self.assertIsNone(report["snapshot"])

    async def test_database_only_context_is_summarized(self):
        context = RepositoryContext(repository="database:app", databases=(
            {"name": "app", "type": "turso", "tables": [], "truncated": False},
        ))
        orchestrator = self.coordinator(MuseOrchestrator(), context)
        with patch("muse.orchestrator.Runner.run", new_callable=AsyncMock,
                   return_value=SimpleNamespace(final_output="No tables observed.")) as runner:
            report = await orchestrator.prepare_repo_context()
        runner.assert_awaited_once()
        self.assertEqual(report["database_count"], 1)
        self.assertIn("database-schema.json", [a["artifact_path"] for a in report["artifacts"]])

    async def test_empty_llm_summary_fails_before_persistence(self):
        orchestrator = self.coordinator(MuseOrchestrator(), RepositoryContext(repository="example/repo"))
        events = []
        with patch("muse.orchestrator.Runner.run", new_callable=AsyncMock,
                   return_value=SimpleNamespace(final_output=" ")), \
             patch.object(orchestrator.repository, "persist_context") as persist:
            with self.assertRaisesRegex(ValueError, "empty architecture summary"):
                await orchestrator.prepare_repo_context(events.append)
        persist.assert_not_called()
        self.assertEqual(events[-1]["status"], "error")

    async def test_single_agent_receives_and_versions_semantic_context(self):
        orchestrator = self.coordinator(MuseOrchestrator(), RepositoryContext(repository="example/repo"))
        with patch("muse.orchestrator.Runner.run", new_callable=AsyncMock,
                   side_effect=[SimpleNamespace(final_output="Architecture."), SimpleNamespace(final_output="Backend.")]) as runner, \
             patch("muse.orchestrator.TursoStore") as store:
            store.return_value.save_agent_output.return_value = {"version": 2}
            result = await orchestrator.run_single_agent(agent_key="backend", run_id="run", specification="spec")
        self.assertIn("Architecture.", runner.call_args_list[1].args[0].instructions)
        self.assertEqual(result["repo_context"]["summary"], "Architecture.")
        self.assertEqual(store.return_value.save_agent_output.call_args.kwargs["agent_name"], "repo_context")

    async def test_full_run_saves_context_report_alongside_outputs(self):
        orchestrator = self.coordinator(MuseOrchestrator(), RepositoryContext(repository="example/repo"))
        with patch("muse.orchestrator.MuseOrchestrator", return_value=orchestrator), \
             patch("muse.orchestrator.Runner.run", new_callable=AsyncMock,
                   return_value=SimpleNamespace(final_output="Architecture.")), \
             patch("muse.orchestrator.TursoStore") as store:
            store.return_value.save_run.return_value = {"run_id": "run"}
            response = await run_project("task", requirement_code="REQ-1", project_name="test")
        self.assertEqual(store.return_value.save_run.call_args.kwargs["outputs"]["repo_context"], response["repo_context"])


class RepoContextApiTests(unittest.TestCase):
    def request_handler(self, stream=True):
        handler = ApiHandler.__new__(ApiHandler)
        handler.allow_post = True
        payload = json.dumps({"request": "task", "requirement_code": "REQ-1", "project_name": "test", "stream": stream}).encode()
        handler.headers = {"Content-Length": str(len(payload))}
        handler.rfile = io.BytesIO(payload)
        handler.wfile = io.BytesIO()
        handler.send_response = lambda status: None
        handler.send_header = lambda key, value: None
        handler.end_headers = lambda: None
        return handler

    def refresh_handler(self, payload):
        base = self.request_handler()
        handler = RepoContextHandler.__new__(RepoContextHandler)
        handler.__dict__.update(base.__dict__)
        body = json.dumps(payload).encode()
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        return handler

    def test_refresh_endpoint_streams_context_only(self):
        handler = self.refresh_handler({"project_id": 7, "stream": True})

        async def refresh(**kwargs):
            kwargs["on_event"]({"type": "repo_context", "status": "checking"})
            kwargs["on_event"]({"type": "repo_context", "status": "ready"})
            return {"repo_context": {"status": "ready", "cache": {"reused": False}}}

        with patch("workflow.refresh_repo_context", side_effect=refresh) as workflow:
            handler.do_POST()
        self.assertEqual(workflow.call_args.kwargs["project_id"], 7)
        events = [json.loads(line) for line in handler.wfile.getvalue().splitlines()]
        self.assertEqual([event["type"] for event in events], ["repo_context", "repo_context", "result"])

    def test_refresh_endpoint_validates_project_id_and_sources(self):
        for payload in [{}, {"project_id": True}, {"project_id": "7"}, {"project_id": -1},
                        {"repository_url": "file:///repo"}]:
            handler = self.refresh_handler(payload)
            with self.subTest(payload=payload), patch("workflow.refresh_repo_context", new_callable=AsyncMock) as workflow:
                handler.do_POST()
                workflow.assert_not_called()
                self.assertIn("error", json.loads(handler.wfile.getvalue()))

    def test_refresh_endpoint_stream_errors_preserve_transport(self):
        handler = self.refresh_handler({"project_id": 7, "stream": True})
        with patch("workflow.refresh_repo_context", new_callable=AsyncMock, side_effect=LookupError("Missing project")):
            handler.do_POST()
        self.assertEqual(json.loads(handler.wfile.getvalue()), {"type": "error", "error": "Missing project"})

    def test_local_refresh_route_dispatches_to_shared_handler(self):
        from web import AgentTeamHandler

        base = self.refresh_handler({"project_id": 7, "stream": True})
        handler = AgentTeamHandler.__new__(AgentTeamHandler)
        handler.__dict__.update(base.__dict__)
        handler.path = "/api/repo-context"
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever)
        thread.start()
        handler.server = SimpleNamespace(event_loop=loop)
        try:
            with patch("workflow.refresh_repo_context", new_callable=AsyncMock,
                       return_value={"repo_context": {"status": "ready"}}):
                handler.do_POST()
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join()
            loop.close()
        event = json.loads(handler.wfile.getvalue())
        self.assertEqual(event["type"], "result")
        self.assertEqual(event["data"]["repo_context"]["status"], "ready")

    def test_serverless_stream_emits_context_before_final_result(self):
        handler = self.request_handler()

        async def workflow(*args, on_event=None, **kwargs):
            on_event({"type": "repo_context", "status": "indexing"})
            on_event({"type": "repo_context", "status": "ready", "data": {"summary": "Architecture."}})
            return {"run_id": "saved"}

        with patch("workflow.run_project", side_effect=workflow):
            handler.do_POST()
        events = [json.loads(line) for line in handler.wfile.getvalue().splitlines()]
        self.assertEqual([event["type"] for event in events], ["repo_context", "repo_context", "result"])
        self.assertEqual(events[-1]["data"]["run_id"], "saved")

    def test_existing_json_clients_remain_compatible(self):
        handler = self.request_handler(stream=False)
        with patch("workflow.run_project", new_callable=AsyncMock, return_value={"run_id": "saved"}) as workflow:
            handler.do_POST()
        self.assertEqual(json.loads(handler.wfile.getvalue()), {"run_id": "saved"})
        self.assertIsNone(workflow.call_args.kwargs["on_event"])

    def test_errors_after_stream_headers_are_ndjson_not_a_second_http_response(self):
        from muse.database_context import DatabaseContextError

        handler = self.request_handler()
        with patch("workflow.run_project", new_callable=AsyncMock, side_effect=DatabaseContextError("Invalid schema source")):
            handler.do_POST()
        self.assertEqual(json.loads(handler.wfile.getvalue()), {"type": "error", "error": "Invalid schema source"})

    def test_local_run_endpoint_uses_same_stream_transport(self):
        from web import AgentTeamHandler

        base = self.request_handler()
        handler = AgentTeamHandler.__new__(AgentTeamHandler)
        handler.__dict__.update(base.__dict__)
        handler.path = "/api/run"
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever)
        thread.start()
        handler.server = SimpleNamespace(event_loop=loop)

        async def workflow(*args, on_event=None, **kwargs):
            on_event({"type": "repo_context", "status": "indexing"})
            on_event({"type": "repo_context", "status": "ready"})
            return {"run_id": "local-saved"}

        try:
            with patch("web.run_project", side_effect=workflow):
                handler.do_POST()
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join()
            loop.close()
        events = [json.loads(line) for line in handler.wfile.getvalue().splitlines()]
        self.assertEqual([event["type"] for event in events], ["repo_context", "repo_context", "result"])
        self.assertEqual(events[-1]["data"]["run_id"], "local-saved")

    def test_local_and_serverless_snapshot_endpoints(self):
        from api.repo_context import handler as SnapshotHandler
        from web import AgentTeamHandler

        snapshot = {"id": 42, "artifacts": [{"artifact_path": "architecture.md", "content": "Architecture."}],
                    "relationships": []}
        for handler_type, module, sender in [(AgentTeamHandler, "web", "_send_json"),
                                             (SnapshotHandler, "api.repo_context", "send_json")]:
            handler = handler_type.__new__(handler_type)
            handler.path = "/api/repo-context?snapshot_id=42"
            with self.subTest(handler=module), patch(f"{module}.load_snapshot", return_value=snapshot) as load, \
                 patch.object(handler, sender) as send:
                handler.do_GET()
                load.assert_called_once_with("snapshot_id=42")
                send.assert_called_once_with(snapshot)


class RepoContextPersistenceTests(unittest.TestCase):
    def test_semantic_artifact_round_trip_and_context_attached_to_old_run(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TursoStore(url="test", token="test")
            store._connect = lambda: sqlite3.connect(str(Path(directory) / "store.db"))
            run = store.save_run(requirement_code="REQ-1", project_name="test", request_text="task",
                                 outputs={"backend": "existing"})
            context = replace(RepositoryContext(repository="example/repo"), architecture_summary="Components: models.py.")
            saved = store.save_repository_context(context=context)
            report = {"status": "ready", "summary": context.architecture_summary, "snapshot": saved}
            first = store.save_agent_output(run_id=run["run_id"], agent_name="repo_context", output=report)
            second = store.save_agent_output(run_id=run["run_id"], agent_name="repo_context", output=report)
            self.assertEqual((first["version"], second["version"]), (1, 2))
            loaded = store.get_run(run["run_id"])
            self.assertEqual(json.loads(loaded["outputs"]["repo_context"]), report)
            with patch("api.repo_context.TursoStore", return_value=store):
                snapshot = load_snapshot(f"snapshot_id={saved['snapshot_id']}")
            architecture = next(a for a in snapshot["artifacts"] if a["artifact_path"] == "architecture.md")
            self.assertIn("Components: models.py.", architecture["content"])
            self.assertIn("architecture_summary", snapshot["context_json"])
            with self.assertRaises(LookupError):
                store.save_agent_output(run_id="missing", agent_name="repo_context", output=report)

    def test_snapshot_id_validation(self):
        for query in ["", "snapshot_id=0", "snapshot_id=-1", "snapshot_id=abc", "snapshot_id=1.5"]:
            with self.subTest(query=query), self.assertRaises(ValueError):
                load_snapshot(query)

    def test_progress_transport_result_errors_and_disconnection(self):
        transport = ProgressStreamMixin()
        transport.wfile = io.BytesIO()
        self.assertFalse(transport.send_progress_result({"run_id": "run"}))
        transport._progress_started = True
        transport.send_progress({"type": "repo_context", "status": "indexing"})
        transport.send_progress_result({"run_id": "run"})
        transport.send_progress_result({"error": "failed"}, 502)
        events = [json.loads(line) for line in transport.wfile.getvalue().splitlines()]
        self.assertEqual([e["type"] for e in events], ["repo_context", "result", "error"])
        transport.wfile = SimpleNamespace(write=lambda content: (_ for _ in ()).throw(BrokenPipeError()))
        transport.send_progress({"type": "repo_context", "status": "ready"})
        self.assertTrue(transport._progress_disconnected)


if __name__ == "__main__":
    unittest.main()
