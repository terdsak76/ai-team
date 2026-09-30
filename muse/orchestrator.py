import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

from agents import Agent, Runner

from muse.dag import TaskDAG
from muse.models import ConflictForecast, MuseTask, Reservation
from muse.reservations import ReservationManager
from muse.repository import RepositoryCoordinator
from muse.task_queue import MuseTaskQueue
from team_agents.backend import backend_agent
from team_agents.frontend import frontend_agent
from team_agents.specification import specification_agent
from team_agents.tester import tester_agent
from team_agents.ui_ux import build_ui_prompt, ui_ux_agent
from turso_store import TursoStore


AGENTS = {
    "specification": specification_agent,
    "ui_ux": ui_ux_agent,
    "frontend": frontend_agent,
    "backend": backend_agent,
    "tester": tester_agent,
}


@dataclass(frozen=True)
class MuseRunResult:
    specification: Any
    ui_design: Any
    frontend: Any
    backend: Any
    test_report: Any
    conflict_forecast: ConflictForecast

    def as_dict(self) -> dict[str, Any]:
        return {
            "specification": self.specification,
            "ui_design": self.ui_design,
            "frontend": self.frontend,
            "backend": self.backend,
            "test_report": self.test_report,
        }


class MuseOrchestrator:
    """Runs the development team through a deterministic task DAG."""

    def __init__(
        self,
        prompts: dict[str, str] | None = None,
        repository_url: str | None = None,
        github_token: str | None = None,
    ):
        self.prompts = prompts or {}
        self.repository = RepositoryCoordinator(repository_url, github_token)
        self.reservations = ReservationManager()
        self._active_reservations: dict[str, Reservation] = {}
        self._effective_system_prompts: dict[str, str] = {}

    def build_dag(self) -> TaskDAG:
        return TaskDAG(
            [
                MuseTask("specification", "Create specification", resources=frozenset({"specification"})),
                MuseTask(
                    "ui_ux",
                    "Create Figma-ready UI specification",
                    dependencies=frozenset({"specification"}),
                    resources=frozenset({"ui_ux"}),
                ),
                MuseTask(
                    "frontend",
                    "Implement frontend",
                    dependencies=frozenset({"specification", "ui_ux"}),
                    resources=frozenset({"frontend"}),
                ),
                MuseTask(
                    "backend",
                    "Implement backend",
                    dependencies=frozenset({"specification"}),
                    resources=frozenset({"backend"}),
                ),
                MuseTask(
                    "tester",
                    "Review implementation",
                    dependencies=frozenset({"frontend", "backend"}),
                    resources=frozenset({"tester"}),
                ),
            ]
        )

    def forecast_conflicts(self, dag: TaskDAG | None = None) -> ConflictForecast:
        return self.reservations.forecast(list((dag or self.build_dag()).tasks))

    async def run(self, user_request: str) -> MuseRunResult:
        repository_instructions = await self.repository.prepare()
        repository_tools = self.repository.tools()
        dag = self.build_dag()
        conflict_forecast = self.forecast_conflicts(dag)
        queue = MuseTaskQueue(list(dag.tasks))
        outputs: dict[str, Any] = {}

        async def execute(task: MuseTask) -> None:
            try:
                self.reservations.reserve(task, self._active_reservations)
                outputs[task.task_id] = await self._execute_task(
                    task.task_id,
                    user_request,
                    outputs,
                    repository_instructions,
                    repository_tools,
                )
                await queue.complete(task.task_id, outputs[task.task_id])
            except BaseException as error:
                await queue.fail(task.task_id, error)
                raise
            finally:
                self.reservations.release(task.task_id, self._active_reservations)

        while await queue.has_pending_or_running():
            ready: list[MuseTask] = []
            while task := await queue.claim_ready():
                ready.append(task)
            if not ready:
                if await queue.has_failed():
                    raise RuntimeError("Muse task queue stopped because a dependency failed.")
                raise RuntimeError("Muse task queue is blocked by an invalid dependency graph.")
            results = await asyncio.gather(*(execute(task) for task in ready), return_exceptions=True)
            errors = [result for result in results if isinstance(result, BaseException)]
            if errors:
                raise errors[0]

        return MuseRunResult(
            specification=outputs["specification"],
            ui_design=outputs["ui_ux"],
            frontend=outputs["frontend"],
            backend=outputs["backend"],
            test_report=outputs["tester"],
            conflict_forecast=conflict_forecast,
        )

    async def _execute_task(
        self,
        task_id: str,
        user_request: str,
        outputs: dict[str, Any],
        repository_instructions: str,
        repository_tools: list,
    ) -> Any:
        agent = self._configured_agent(task_id, repository_instructions, repository_tools)
        if task_id == "specification":
            return (await Runner.run(agent, user_request)).final_output

        specification = outputs["specification"]
        spec_json = (
            specification.model_dump_json(indent=2)
            if hasattr(specification, "model_dump_json")
            else str(specification)
        )
        if task_id == "ui_ux":
            prompt = build_ui_prompt(
                spec=outputs["specification"],
                repo_context=repository_instructions,
            )
        elif task_id == "frontend":
            prompt = f"""
Implement the frontend portion of this approved specification using the
approved Figma-ready UI specification.

SPECIFICATION:

{spec_json}

FIGMA-READY UI SPECIFICATION:

{outputs["ui_ux"]}
"""
        elif task_id == "backend":
            prompt = f"""
Implement the backend portion of this approved specification.

SPECIFICATION:

{spec_json}
"""
        else:
            prompt = f"""
Review the implementation against this specification.

SPECIFICATION:

{spec_json}

FIGMA-READY UI SPECIFICATION:

{outputs["ui_ux"]}

FRONTEND IMPLEMENTATION:

{outputs["frontend"]}

BACKEND IMPLEMENTATION:

{outputs["backend"]}
"""
        return (await Runner.run(agent, prompt)).final_output

    async def run_single_agent(
        self,
        *,
        agent_key: str,
        run_id: str,
        specification: Any,
        ui_design: Any = "",
    ) -> dict[str, Any]:
        """Run one implementation/design agent from a saved specification."""
        if agent_key not in {"ui_ux", "frontend", "backend"}:
            raise ValueError("Only ui_ux, frontend, and backend can be run independently.")

        repository_instructions = await self.repository.prepare()
        output = await self._execute_task(
            agent_key,
            "",
            {
                "specification": specification,
                "ui_ux": ui_design or "No existing UI/UX design was supplied. Preserve the repository conventions.",
            },
            repository_instructions,
            self.repository.tools(),
        )
        saved = TursoStore().save_agent_output(
            run_id=run_id,
            agent_name=agent_key,
            output=output,
            system_prompt=self._effective_system_prompts.get(agent_key, ""),
        )
        return {"output": output, **saved}

    def _configured_agent(self, key: str, repository_instructions: str, repository_tools: list) -> Agent:
        agent = AGENTS[key]
        instructions = self.prompts.get(key, agent.instructions)
        if not isinstance(instructions, str) or not instructions.strip():
            raise ValueError(f"The {key} agent prompt cannot be empty.")
        effective_instructions = instructions + repository_instructions
        self._effective_system_prompts[key] = effective_instructions
        return agent.clone(
            instructions=effective_instructions,
            tools=[*agent.tools, *repository_tools],
        )


def get_default_prompts() -> dict[str, str]:
    return {
        key: agent.instructions
        for key, agent in AGENTS.items()
        if isinstance(agent.instructions, str)
    }


async def run_project(
    user_request: str,
    prompts: dict[str, str] | None = None,
    repository_url: str | None = None,
    requirement_code: str | None = None,
    project_name: str | None = None,
    github_token: str | None = None,
) -> dict[str, Any]:
    """Compatibility entrypoint used by the web and Vercel handlers."""
    orchestrator = MuseOrchestrator(prompts, repository_url, github_token)
    result = await orchestrator.run(user_request)
    saved = TursoStore().save_run(
        requirement_code=requirement_code.strip() if requirement_code and requirement_code.strip() else f"REQ-{uuid.uuid4().hex[:8].upper()}",
        project_name=project_name.strip() if project_name and project_name.strip() else "Unnamed project",
        request_text=user_request,
        outputs={
            "specification": result.specification,
            "ui_ux": result.ui_design,
            "frontend": result.frontend,
            "backend": result.backend,
            "tester": result.test_report,
        },
        system_prompts=orchestrator._effective_system_prompts,
    )
    response = result.as_dict()
    response.update(saved)
    return response
