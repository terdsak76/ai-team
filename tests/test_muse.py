import asyncio
import unittest

from muse.dag import TaskDAG
from muse.models import MuseTask
from muse.orchestrator import MuseOrchestrator
from muse.reservations import ReservationManager
from muse.task_queue import MuseTaskQueue


class MuseArchitectureTests(unittest.TestCase):
    def test_ui_design_gates_frontend_while_backend_stays_parallel(self):
        tasks = {task.task_id: task for task in MuseOrchestrator().build_dag().tasks}

        self.assertEqual(tasks["ui_ux"].dependencies, frozenset({"specification"}))
        self.assertEqual(tasks["frontend"].dependencies, frozenset({"specification", "ui_ux"}))
        self.assertEqual(tasks["backend"].dependencies, frozenset({"specification"}))
        self.assertEqual(tasks["tester"].dependencies, frozenset({"frontend", "backend"}))

    def test_dag_rejects_cycles(self):
        tasks = [
            MuseTask("a", "A", dependencies=frozenset({"b"})),
            MuseTask("b", "B", dependencies=frozenset({"a"})),
        ]
        with self.assertRaisesRegex(ValueError, "cycle"):
            TaskDAG(tasks)

    def test_queue_releases_parallel_tasks_after_specification(self):
        async def scenario():
            queue = MuseTaskQueue(
                [
                    MuseTask("specification", "Specification"),
                    MuseTask("frontend", "Frontend", frozenset({"specification"})),
                    MuseTask("backend", "Backend", frozenset({"specification"})),
                ]
            )
            first = await queue.claim_ready()
            self.assertEqual(first.task_id, "specification")
            self.assertIsNone(await queue.claim_ready())
            await queue.complete("specification", "spec")
            ready = {(
                (await queue.claim_ready()).task_id,
                (await queue.claim_ready()).task_id,
            )}
            self.assertEqual(ready, {("frontend", "backend")})

        asyncio.run(scenario())

    def test_reservation_manager_forecasts_shared_resources(self):
        tasks = [
            MuseTask("frontend", "Frontend", resources=frozenset({"api-contract"})),
            MuseTask("backend", "Backend", resources=frozenset({"api-contract"})),
        ]
        forecast = ReservationManager().forecast(tasks)
        self.assertTrue(forecast.has_conflicts)
        self.assertEqual(forecast.conflicts[0].task_ids, ("frontend", "backend"))


if __name__ == "__main__":
    unittest.main()
