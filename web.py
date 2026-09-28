import asyncio
import json
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from github_repository import GitHubRepositoryError, parse_github_repository
from workflow import get_default_prompts, run_project


HOST = "127.0.0.1"
PORT = 8000
MAX_REQUEST_BYTES = 150_000
PROMPT_KEYS = {"specification", "frontend", "backend", "tester"}
WEB_ROOT = Path(__file__).parent / "web"


def to_json_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


class AgentTeamHandler(BaseHTTPRequestHandler):
    server_version = "AgentTeamUI/1.0"

    def do_GET(self):
        if self.path == "/api/prompts":
            self._send_json({"prompts": get_default_prompts()})
            return

        static_files = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/styles.css": ("styles.css", "text/css; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
        }
        file_entry = static_files.get(self.path)
        if file_entry is None:
            self._send_json({"error": "Not found."}, status=404)
            return

        filename, content_type = file_entry
        try:
            content = (WEB_ROOT / filename).read_bytes()
        except OSError:
            self._send_json({"error": "The web interface could not be loaded."}, status=500)
            return

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self):
        if self.path != "/api/run":
            self._send_json({"error": "Not found."}, status=404)
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json({"error": "Invalid Content-Length."}, status=400)
            return
        if content_length <= 0 or content_length > MAX_REQUEST_BYTES:
            self._send_json({"error": "Request body is empty or too large."}, status=413)
            return

        try:
            payload = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json({"error": "Request body must be valid JSON."}, status=400)
            return
        if not isinstance(payload, dict):
            self._send_json({"error": "Request body must be a JSON object."}, status=400)
            return

        user_request = payload.get("request")
        prompts = payload.get("prompts", {})
        repository_url = payload.get("repository_url", "")
        if not isinstance(user_request, str) or not user_request.strip():
            self._send_json({"error": "Describe the task before starting the agents."}, status=400)
            return
        if len(user_request) > 20_000:
            self._send_json({"error": "The task description must be 20,000 characters or fewer."}, status=400)
            return
        if not isinstance(prompts, dict) or set(prompts) - PROMPT_KEYS:
            self._send_json({"error": "Prompts must contain valid agent names and text values."}, status=400)
            return
        if any(not isinstance(value, str) or not value.strip() for value in prompts.values()):
            self._send_json({"error": "Each agent prompt must contain text."}, status=400)
            return
        if any(len(value) > 30_000 for value in prompts.values()):
            self._send_json({"error": "Agent prompts must be 30,000 characters or fewer."}, status=400)
            return
        if not isinstance(repository_url, str) or len(repository_url) > 500:
            self._send_json({"error": "The GitHub repository URL is invalid."}, status=400)
            return
        repository_url = repository_url.strip()
        if repository_url:
            try:
                parse_github_repository(repository_url)
            except GitHubRepositoryError as error:
                self._send_json({"error": str(error)}, status=400)
                return

        try:
            future = asyncio.run_coroutine_threadsafe(
                run_project(user_request.strip(), prompts, repository_url or None),
                self.server.event_loop,
            )
            result = future.result()
        except GitHubRepositoryError as error:
            self._send_json(
                {"error": f"GitHub repository access failed: {error}"},
                status=502,
            )
            return
        except Exception:
            traceback.print_exc()
            self._send_json(
                {"error": "The agent workflow failed. Check the server console for details."},
                status=502,
            )
            return

        self._send_json({key: to_json_value(value) for key, value in result.items()})

    def _send_json(self, payload: dict[str, Any], status: int = 200):
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args):
        print(f"{self.address_string()} - {format % args}")


def main():
    event_loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=event_loop.run_forever, daemon=True)
    loop_thread.start()

    server = ThreadingHTTPServer((HOST, PORT), AgentTeamHandler)
    server.event_loop = event_loop
    print(f"Agent team UI running at http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down agent team UI.")
    finally:
        event_loop.call_soon_threadsafe(event_loop.stop)
        loop_thread.join()
        event_loop.close()
        server.server_close()


if __name__ == "__main__":
    main()
