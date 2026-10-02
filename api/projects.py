import json
import traceback
from http.server import BaseHTTPRequestHandler
from typing import Any

from github_repository import GitHubRepositoryError, parse_github_repository
from turso_store import TursoConfigurationError, TursoStore
from workflow import get_default_prompts
from muse.database_context import validate_database_connections


PROMPT_KEYS = {"specification", "ui_ux", "frontend", "backend", "tester"}
MAX_REQUEST_BYTES = 150_000


def public_project(project: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in project.items() if key != "github_token"}


def read_payload(handler: BaseHTTPRequestHandler) -> dict[str, Any] | None:
    try:
        content_length = int(handler.headers.get("Content-Length", "0"))
        if content_length <= 0 or content_length > MAX_REQUEST_BYTES:
            raise ValueError("Project request body is empty or too large.")
        payload = json.loads(handler.rfile.read(content_length))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as error:
        handler.send_json({"error": str(error) or "Project data must be valid JSON."}, status=400)
        return None
    if not isinstance(payload, dict):
        handler.send_json({"error": "Project data must be a JSON object."}, status=400)
        return None
    return payload


def project_fields(payload: dict[str, Any], allow_blank_token: bool = False) -> dict[str, Any]:
    project_name = payload.get("project_name", "")
    github_repo = payload.get("github_repo", "")
    github_token = payload.get("github_token", "")
    project_context = payload.get("project_context", "")
    prompts = payload.get("system_prompts", get_default_prompts())
    if not isinstance(project_name, str) or not project_name.strip() or len(project_name) > 200:
        raise ValueError("A project name of 200 characters or fewer is required.")
    if not isinstance(github_repo, str) or len(github_repo) > 500:
        raise ValueError("The GitHub repository URL is invalid.")
    github_repo = github_repo.strip()
    if github_repo:
        parse_github_repository(github_repo)
    if not isinstance(github_token, str) or len(github_token) > 500:
        raise ValueError("The GitHub token is invalid.")
    if not isinstance(project_context, str) or len(project_context) > 20_000:
        raise ValueError("Project context must be 20,000 characters or fewer.")
    if not isinstance(prompts, dict) or set(prompts) != PROMPT_KEYS:
        raise ValueError("Each agent system prompt is required.")
    if any(not isinstance(value, str) or not value.strip() for value in prompts.values()):
        raise ValueError("Each agent system prompt must contain text.")
    if any(len(value) > 30_000 for value in prompts.values()):
        raise ValueError("Agent prompts must be 30,000 characters or fewer.")
    return {
        "project_name": project_name.strip(),
        "github_repo": github_repo,
        "github_token": github_token if (github_token or not allow_blank_token) else None,
        "project_context": project_context.strip(),
        "clear_github_token": bool(payload.get("clear_github_token", False)),
        "system_prompts": prompts,
        "database_connections": validate_database_connections(payload["database_connections"])
        if "database_connections" in payload else None,
    }


class handler(BaseHTTPRequestHandler):
    def send_json(self, payload: dict[str, Any], status: int = 200):
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        try:
            self.send_json({"items": [public_project(item) for item in TursoStore().list_projects()]})
        except TursoConfigurationError as error:
            self.send_json({"error": str(error)}, status=503)
        except Exception:
            traceback.print_exc()
            self.send_json({"error": "The projects could not be loaded."}, status=502)

    def do_POST(self):
        payload = read_payload(self)
        if payload is None:
            return
        try:
            fields = project_fields(payload)
            fields.pop("clear_github_token", None)
            saved = TursoStore().create_project(**fields)
        except (ValueError, GitHubRepositoryError) as error:
            self.send_json({"error": str(error)}, status=400)
            return
        except TursoConfigurationError as error:
            self.send_json({"error": str(error)}, status=503)
            return
        except Exception as error:
            if "UNIQUE constraint failed" in str(error):
                self.send_json({"error": "A project with that name already exists."}, status=409)
                return
            traceback.print_exc()
            self.send_json({"error": "The project could not be saved."}, status=502)
            return
        self.send_json(public_project(saved), status=201)

    def do_PUT(self):
        project_id = self.path.rstrip("/").rsplit("/", 1)[-1]
        try:
            project_id = int(project_id)
        except ValueError:
            self.send_json({"error": "The selected project is invalid."}, status=400)
            return
        payload = read_payload(self)
        if payload is None:
            return
        try:
            fields = project_fields(payload, allow_blank_token=True)
            saved = TursoStore().update_project(project_id=project_id, **fields)
        except (ValueError, GitHubRepositoryError) as error:
            self.send_json({"error": str(error)}, status=400)
            return
        except LookupError as error:
            self.send_json({"error": str(error)}, status=404)
            return
        except TursoConfigurationError as error:
            self.send_json({"error": str(error)}, status=503)
            return
        except Exception as error:
            if "UNIQUE constraint failed" in str(error):
                self.send_json({"error": "A project with that name already exists."}, status=409)
                return
            traceback.print_exc()
            self.send_json({"error": "The project could not be saved."}, status=502)
            return
        self.send_json(public_project(saved))

    def log_message(self, format: str, *args):
        print(f"{self.address_string()} - {format % args}")
