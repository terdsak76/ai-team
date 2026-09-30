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
        project_context: str = "",
        project_memories: list[dict[str, Any]] | None = None,
    ):
        self.prompts = prompts or {}
        self.repository = RepositoryCoordinator(repository_url, github_token)
        self.project_context = project_context.strip()
        self.project_memories = project_memories or []
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
            prompt = f"""
PROJECT CONTEXT:
{self.project_context or "No saved project context is available."}

RELEVANT PROJECT MEMORY:
{self._format_project_memories()}

NEW TASK:
{user_request}

Use the project context and memories as background only. Prioritize the new
task and identify any conflicts instead of silently assuming that old memory
is still correct.
"""
            return (await Runner.run(agent, prompt)).final_output

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

    def _format_project_memories(self) -> str:
        if not self.project_memories:
            return "No relevant project memories were found."
        lines: list[str] = []
        total_chars = 0
        for memory in self.project_memories:
            line = f"- [{memory.get('memory_type', 'memory')}] {memory.get('content', '')[:2_000]}"
            if total_chars + len(line) > 12_000:
                break
            lines.append(line)
            total_chars += len(line)
        return "\n".join(lines) or "No relevant project memories were found."

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
    project_id: int | None = None,
) -> dict[str, Any]:
    """Compatibility entrypoint used by the web and Vercel handlers."""
    store = TursoStore()
    project_context = ""
    project_memories: list[dict[str, Any]] = []
    if project_id is not None:
        project = store.get_project(project_id)
        project_context = project["project_context"][:8_000]
        project_memories = store.get_relevant_project_memories(
            project_id=project_id,
            task_text=user_request,
        )
    orchestrator = MuseOrchestrator(
        prompts,
        repository_url,
        github_token,
        project_context,
        project_memories,
    )
    result = await orchestrator.run(user_request)
    saved = store.save_run(
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
    if project_id is not None:
        store.create_project_memory(
            project_id=project_id,
            memory_type="task_summary",
            content=_build_task_memory(user_request, result.specification),
            tags=_memory_tags(result.specification),
            source_run_id=saved["run_id"],
        )
    response = result.as_dict()
    response.update(saved)
    return response


def _build_task_memory(user_request: str, specification: Any) -> str:
    """Create a compact durable memory from the approved specification."""
    if hasattr(specification, "model_dump"):
        data = specification.model_dump(mode="json")
    else:
        return f"Completed task: {user_request[:1000]}"

    lines = [
        f"Feature: {data.get('feature_name', '')}",
        f"Summary: {data.get('summary', '')}",
    ]
    for label, key in (
        ("Frontend", "frontend_tasks"),
        ("Backend", "backend_tasks"),
        ("Assumptions", "assumptions"),
        ("Open questions", "open_questions"),
    ):
        values = data.get(key) or []
        if values:
            lines.append(f"{label}: " + "; ".join(str(value) for value in values[:4]))
    return "\n".join(lines)[:4_000]


def _memory_tags(specification: Any) -> str:
    if hasattr(specification, "model_dump"):
        data = specification.model_dump(mode="json")
        return str(data.get("feature_name", ""))[:200]
    return ""
