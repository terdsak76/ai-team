import asyncio
import json
import traceback

from api._handler import MAX_REQUEST_BYTES, PROMPT_KEYS, ApiHandler
from github_repository import GitHubRepositoryError, parse_github_repository
from turso_store import TursoConfigurationError, TursoStore


class handler(ApiHandler):
    allow_post = True

    def do_POST(self):
        if not self.allow_post:
            self.send_json({"error": "Not found."}, status=404)
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json({"error": "Invalid Content-Length."}, status=400)
            return
        if content_length <= 0 or content_length > MAX_REQUEST_BYTES:
            self.send_json({"error": "Request body is empty or too large."}, status=413)
            return
        try:
            payload = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_json({"error": "Request body must be valid JSON."}, status=400)
            return
        if not isinstance(payload, dict):
            self.send_json({"error": "Request body must be a JSON object."}, status=400)
            return

        agent_key = payload.get("agent")
        run_id = payload.get("run_id")
        specification = payload.get("specification", "")
        ui_design = payload.get("ui_design", "")
        prompts = payload.get("prompts", {})
        repository_url = payload.get("repository_url", "")
        if agent_key not in {"ui_ux", "frontend", "backend"}:
            self.send_json({"error": "Choose ui_ux, frontend, or backend."}, status=400)
            return
        if not isinstance(run_id, str) or not run_id.strip():
            self.send_json({"error": "A saved run_id is required."}, status=400)
            return
        if not isinstance(prompts, dict) or set(prompts) - PROMPT_KEYS:
            self.send_json({"error": "Prompts must contain valid agent names and text values."}, status=400)
            return
        if any(not isinstance(value, str) or not value.strip() for value in prompts.values()):
            self.send_json({"error": "Each agent prompt must contain text."}, status=400)
            return
        if not isinstance(repository_url, str) or len(repository_url) > 500:
            self.send_json({"error": "The GitHub repository URL is invalid."}, status=400)
            return
        repository_url = repository_url.strip()
        try:
            if repository_url:
                parse_github_repository(repository_url)
            store = TursoStore()
            context = store.get_run(run_id.strip())
            specification = specification.strip() if isinstance(specification, str) else ""
            ui_design = ui_design.strip() if isinstance(ui_design, str) else ""
            if not specification:
                specification = context["outputs"].get("specification", "")
            if not ui_design:
                ui_design = context["outputs"].get("ui_ux", "")
            if not specification:
                self.send_json({"error": "The selected run has no specification output."}, status=400)
                return
            if specification != context["outputs"].get("specification", ""):
                store.update_specification(run_id=run_id.strip(), specification=specification)
            from workflow import run_single_agent

            result = asyncio.run(
                run_single_agent(
                    agent_key=agent_key,
                    run_id=run_id.strip(),
                    specification=specification,
                    ui_design=ui_design,
                    prompts=prompts,
                    repository_url=repository_url or None,
                )
            )
        except (ValueError, GitHubRepositoryError) as error:
            self.send_json({"error": str(error)}, status=400)
            return
        except LookupError as error:
            self.send_json({"error": str(error)}, status=404)
            return
        except TursoConfigurationError as error:
            self.send_json({"error": str(error)}, status=503)
            return
        except Exception:
            traceback.print_exc()
            self.send_json({"error": "The selected agent run failed. Check the function logs for details."}, status=502)
            return
        self.send_json({"key": value for key, value in result.items()})
