import asyncio
import json
import traceback
from http.server import BaseHTTPRequestHandler
from typing import Any

from github_repository import GitHubRepositoryError, parse_github_repository
from turso_store import TursoConfigurationError, TursoStore
from muse.database_context import DatabaseContextError
from api._progress import ProgressStreamMixin

MAX_REQUEST_BYTES = 150_000
PROMPT_KEYS = {"specification", "ui_ux", "frontend", "backend", "tester"}


def to_json_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


class ApiHandler(ProgressStreamMixin, BaseHTTPRequestHandler):
    allow_get = False
    allow_post = False
    allow_patch = False

    def send_json(self, payload: dict[str, Any], status: int = 200):
        if self.send_progress_result(payload, status):
            return
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        if not self.allow_get:
            self.send_json({"error": "Not found."}, status=404)
            return
        try:
            from workflow import get_default_prompts

            self.send_json({"prompts": get_default_prompts()})
        except Exception as error:
            self._handle_configuration_error(error)

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

        user_request = payload.get("request")
        prompts = payload.get("prompts", {})
        repository_url = payload.get("repository_url", "")
        requirement_code = payload.get("requirement_code", "")
        project_name = payload.get("project_name", "")
        project_id = payload.get("project_id")
        github_token = None
        if project_id is not None:
            try:
                if isinstance(project_id, bool):
                    raise ValueError
                project_id = int(project_id)
                project = TursoStore().get_project(project_id)
            except (TypeError, ValueError):
                self.send_json({"error": "The selected project is invalid."}, status=400)
                return
            except LookupError as error:
                self.send_json({"error": str(error)}, status=404)
                return
            project_name = project["project_name"]
            repository_url = project["github_repo"]
            github_token = project["github_token"]
            if not isinstance(prompts, dict) or not prompts:
                prompts = project["system_prompts"]
        if not isinstance(user_request, str) or not user_request.strip():
            self.send_json({"error": "Describe the task before starting the agents."}, status=400)
            return
        if len(user_request) > 20_000:
            self.send_json({"error": "The task description must be 20,000 characters or fewer."}, status=400)
            return
        if not isinstance(requirement_code, str) or not requirement_code.strip() or len(requirement_code) > 120:
            self.send_json({"error": "A requirement code of 120 characters or fewer is required."}, status=400)
            return
        if not isinstance(project_name, str) or not project_name.strip() or len(project_name) > 200:
            self.send_json({"error": "A project name of 200 characters or fewer is required."}, status=400)
            return
        if not isinstance(prompts, dict) or set(prompts) - PROMPT_KEYS:
            self.send_json({"error": "Prompts must contain valid agent names and text values."}, status=400)
            return
        if any(not isinstance(value, str) or not value.strip() for value in prompts.values()):
            self.send_json({"error": "Each agent prompt must contain text."}, status=400)
            return
        if any(len(value) > 30_000 for value in prompts.values()):
            self.send_json({"error": "Agent prompts must be 30,000 characters or fewer."}, status=400)
            return
        if not isinstance(repository_url, str) or len(repository_url) > 500:
            self.send_json({"error": "The GitHub repository URL is invalid."}, status=400)
            return
        repository_url = repository_url.strip()
        if repository_url:
            try:
                parse_github_repository(repository_url)
            except GitHubRepositoryError as error:
                self.send_json({"error": str(error)}, status=400)
                return
        try:
            from workflow import run_project
        except Exception as error:
            self._handle_configuration_error(error)
            return

        if payload.get("stream") is True:
            self.start_progress_stream()
        try:
            result = asyncio.run(
                run_project(
                    user_request.strip(),
                    prompts,
                    repository_url or None,
                    requirement_code.strip(),
                    project_name.strip(),
                    github_token,
                    project_id=project_id,
                    database_connections=payload.get("database_connections"),
                    on_event=self.send_progress if payload.get("stream") is True else None,
                    force_refresh_context=payload.get("force_refresh_context") is True,
                )
            )
        except DatabaseContextError as error:
            self.send_json({"error": str(error)}, status=400)
            return
        except GitHubRepositoryError as error:
            self.send_json({"error": f"GitHub repository access failed: {error}"}, status=502)
            return
        except TursoConfigurationError as error:
            self.send_json({"error": str(error)}, status=503)
            return
        except Exception:
            traceback.print_exc()
            self.send_json({"error": "The agent workflow failed. Check the function logs for details."}, status=502)
            return
        self.send_json({key: to_json_value(value) for key, value in result.items()})

    def do_PATCH(self):
        if not self.allow_patch:
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
        run_id = payload.get("run_id") if isinstance(payload, dict) else None
        specification = payload.get("specification") if isinstance(payload, dict) else None
        if not isinstance(run_id, str) or not run_id.strip():
            self.send_json({"error": "A run_id is required."}, status=400)
            return
        if not isinstance(specification, str) or not specification.strip():
            self.send_json({"error": "The specification cannot be empty."}, status=400)
            return
        try:
            saved = TursoStore().update_specification(
                run_id=run_id.strip(),
                specification=specification,
            )
        except LookupError as error:
            self.send_json({"error": str(error)}, status=404)
            return
        except (ValueError, TursoConfigurationError) as error:
            status = 400 if isinstance(error, ValueError) else 503
            self.send_json({"error": str(error)}, status=status)
            return
        except Exception:
            traceback.print_exc()
            self.send_json({"error": "The specification could not be saved."}, status=502)
            return
        self.send_json(saved)

    def _handle_configuration_error(self, error: Exception):
        traceback.print_exc()
        if isinstance(error, RuntimeError) and "OPENROUTER_API_KEY" in str(error):
            self.send_json({"error": "OPENROUTER_API_KEY is not configured for this Vercel deployment."}, status=500)
            return
        self.send_json({"error": "The server could not load its agent configuration. Check the function logs."}, status=500)

    def log_message(self, format: str, *args):
        print(f"{self.address_string()} - {format % args}")
