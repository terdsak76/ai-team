"""Backward-compatible workflow imports for the web and serverless APIs."""

from muse.orchestrator import MuseOrchestrator, get_default_prompts, run_project


async def run_single_agent(
    *,
    agent_key: str,
    run_id: str,
    specification: str,
    ui_design: str = "",
    prompts: dict[str, str] | None = None,
    repository_url: str | None = None,
):
    """Run one agent using a saved or user-edited specification."""
    return await MuseOrchestrator(prompts, repository_url).run_single_agent(
        agent_key=agent_key,
        run_id=run_id,
        specification=specification,
        ui_design=ui_design,
    )

__all__ = ["get_default_prompts", "run_project", "run_single_agent"]
