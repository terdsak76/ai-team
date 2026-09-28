import asyncio
import json
import traceback
from http.server import BaseHTTPRequestHandler
from typing import Any

from github_repository import GitHubRepositoryError, parse_github_repository

MAX_REQUEST_BYTES = 150_000
PROMPT_KEYS = {"specification", "frontend", "backend", "tester"}


def to_json_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


class ApiHandler(BaseHTTPRequestHandler):
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
        if self.path.rstrip("/") in {"", "/api", "/api/prompts"}:
            try:
                from workflow import get_default_prompts

                self.send_json({"prompts": get_default_prompts()})
            except Exception as error:
                self._handle_configuration_error(error)
            return
        self.send_json({"error": "Not found."}, status=404)

    def do_POST(self):
        if self.path.rstrip("/") not in {"", "/api/run", "/run"}:
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
        if not isinstance(user_request, str) or not user_request.strip():
            self.send_json({"error": "Describe the task before starting the agents."}, status=400)
            return
        if len(user_request) > 20_000:
            self.send_json({"error": "The task description must be 20,000 characters or fewer."}, status=400)
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

        try:
            result = asyncio.run(run_project(user_request.strip(), prompts, repository_url or None))
        except GitHubRepositoryError as error:
            self.send_json({"error": f"GitHub repository access failed: {error}"}, status=502)
            return
        except Exception:
            traceback.print_exc()
            self.send_json({"error": "The agent workflow failed. Check the function logs for details."}, status=502)
            return
        self.send_json({key: to_json_value(value) for key, value in result.items()})

    def _handle_configuration_error(self, error: Exception):
        traceback.print_exc()
        if isinstance(error, RuntimeError) and "OPENROUTER_API_KEY" in str(error):
            self.send_json({"error": "OPENROUTER_API_KEY is not configured for this Vercel deployment."}, status=500)
            return
        self.send_json({"error": "The server could not load its agent configuration. Check the function logs."}, status=500)

    def log_message(self, format: str, *args):
        print(f"{self.address_string()} - {format % args}")
