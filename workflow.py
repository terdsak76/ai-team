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
    github_token: str | None = None,
    project_id: int | None = None,
    database_connections: list[dict] | None = None,
    force_refresh_context: bool = False,
):
    """Run one agent using a saved or user-edited specification."""
    if project_id is not None:
        from turso_store import TursoStore

        project = TursoStore().get_project(project_id)
        if database_connections is None:
            database_connections = project.get("database_connections", [])
        if repository_url is None:
            repository_url = project["github_repo"] or None
        if github_token is None:
            github_token = project["github_token"] or None
        if prompts is None:
            prompts = project["system_prompts"]
    return await MuseOrchestrator(
        prompts,
        repository_url,
        github_token,
        project_id=project_id,
        database_connections=database_connections,
    ).run_single_agent(
        agent_key=agent_key,
        run_id=run_id,
        specification=specification,
        ui_design=ui_design,
        force_refresh_context=force_refresh_context,
    )


async def refresh_repo_context(*, project_id: int | None = None, repository_url: str | None = None,
                               database_connections: list[dict] | None = None, on_event=None):
    """Refresh the project's current cache without running development agents."""
    github_token = None
    if project_id is not None:
        from turso_store import TursoStore

        project = TursoStore().get_project(project_id)
        repository_url = project["github_repo"] or None
        github_token = project["github_token"] or None
        database_connections = project.get("database_connections", [])
    orchestrator = MuseOrchestrator(repository_url=repository_url, github_token=github_token,
                                    project_id=project_id, database_connections=database_connections)
    report = await orchestrator.prepare_repo_context(on_event, force_refresh=True)
    return {"repo_context": report}


__all__ = ["get_default_prompts", "run_project", "run_single_agent", "refresh_repo_context"]
