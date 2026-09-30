import asyncio
import json
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from github_repository import GitHubRepositoryError, parse_github_repository
from turso_store import TursoConfigurationError, TursoStore
from workflow import get_default_prompts, run_project, run_single_agent


HOST = "127.0.0.1"
PORT = 8000
MAX_REQUEST_BYTES = 150_000
PROMPT_KEYS = {"specification", "ui_ux", "frontend", "backend", "tester"}
WEB_ROOT = Path(__file__).parent / "web"


def to_json_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def public_project(project: dict[str, Any]) -> dict[str, Any]:
    """Return project data without exposing the stored GitHub token."""
    return {key: value for key, value in project.items() if key != "github_token"}


class AgentTeamHandler(BaseHTTPRequestHandler):
    server_version = "AgentTeamUI/1.0"

    def do_GET(self):
        parsed_path = urlparse(self.path)
        if parsed_path.path == "/api/prompts":
            self._send_json({"prompts": get_default_prompts()})
            return
        if parsed_path.path == "/api/projects":
            try:
                self._send_json({"items": [public_project(item) for item in TursoStore().list_projects()]})
            except TursoConfigurationError as error:
                self._send_json({"error": str(error)}, status=503)
            except Exception:
                traceback.print_exc()
                self._send_json({"error": "The projects could not be loaded."}, status=502)
            return
        if parsed_path.path == "/api/outputs":
            params = parse_qs(parsed_path.query)
            requirement_code = params.get("requirement_code", [""])[0].strip()
            project_name = params.get("project_name", [""])[0].strip()
            try:
                outputs = TursoStore().query_runs(
                    requirement_code=requirement_code or None,
                    project_name=project_name or None,
                )
            except ValueError as error:
                self._send_json({"error": str(error)}, status=400)
            except Exception:
                traceback.print_exc()
                self._send_json({"error": "The saved outputs could not be loaded."}, status=502)
            else:
                self._send_json({"items": outputs})
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
        if urlparse(self.path).path == "/api/projects":
            self._save_project()
            return
        if urlparse(self.path).path == "/api/agent":
            self._run_single_agent()
            return
        if urlparse(self.path).path != "/api/run":
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
                self._send_json({"error": "The selected project is invalid."}, status=400)
                return
            except LookupError as error:
                self._send_json({"error": str(error)}, status=404)
                return
            project_name = project["project_name"]
            repository_url = project["github_repo"]
            github_token = project["github_token"]
            if not isinstance(prompts, dict) or not prompts:
                prompts = project["system_prompts"]
        if not isinstance(user_request, str) or not user_request.strip():
            self._send_json({"error": "Describe the task before starting the agents."}, status=400)
            return
        if len(user_request) > 20_000:
            self._send_json({"error": "The task description must be 20,000 characters or fewer."}, status=400)
            return
        if not isinstance(requirement_code, str) or not requirement_code.strip() or len(requirement_code) > 120:
            self._send_json({"error": "A requirement code of 120 characters or fewer is required."}, status=400)
            return
        if not isinstance(project_name, str) or not project_name.strip() or len(project_name) > 200:
            self._send_json({"error": "A project name of 200 characters or fewer is required."}, status=400)
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
                run_project(
                    user_request.strip(),
                    prompts,
                    repository_url or None,
                    requirement_code.strip(),
                    project_name.strip(),
                    github_token,
                    project_id=project_id,
                ),
                self.server.event_loop,
            )
            result = future.result()
        except GitHubRepositoryError as error:
            self._send_json(
                {"error": f"GitHub repository access failed: {error}"},
                status=502,
            )
            return
        except TursoConfigurationError as error:
            self._send_json({"error": str(error)}, status=503)
            return
        except Exception:
            traceback.print_exc()
            self._send_json(
                {"error": "The agent workflow failed. Check the server console for details."},
                status=502,
            )
            return

        self._send_json({key: to_json_value(value) for key, value in result.items()})

    def _run_single_agent(self):
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

        agent_key = payload.get("agent")
        run_id = payload.get("run_id")
        specification = payload.get("specification", "")
        ui_design = payload.get("ui_design", "")
        prompts = payload.get("prompts", {})
        repository_url = payload.get("repository_url", "")
        project_id = payload.get("project_id")
        github_token = None
        if agent_key not in {"ui_ux", "frontend", "backend"}:
            self._send_json({"error": "Choose ui_ux, frontend, or backend."}, status=400)
            return
        if not isinstance(run_id, str) or not run_id.strip():
            self._send_json({"error": "A saved run_id is required."}, status=400)
            return
        try:
            if project_id is not None:
                if isinstance(project_id, bool):
                    raise ValueError("The selected project is invalid.")
                project = TursoStore().get_project(int(project_id))
                if not isinstance(prompts, dict) or not prompts:
                    prompts = project["system_prompts"]
                repository_url = project["github_repo"]
                github_token = project["github_token"]
            context = TursoStore().get_run(run_id.strip())
            specification = specification.strip() if isinstance(specification, str) else ""
            ui_design = ui_design.strip() if isinstance(ui_design, str) else ""
            if not specification:
                specification = context["outputs"].get("specification", "")
            if not ui_design:
                ui_design = context["outputs"].get("ui_ux", "")
            if not specification:
                self._send_json({"error": "The selected run has no specification output."}, status=400)
                return
            if specification != context["outputs"].get("specification", ""):
                TursoStore().update_specification(
                    run_id=run_id.strip(),
                    specification=specification,
                )
            if not isinstance(prompts, dict) or set(prompts) - PROMPT_KEYS:
                self._send_json({"error": "Prompts must contain valid agent names and text values."}, status=400)
                return
            if any(not isinstance(value, str) or not value.strip() for value in prompts.values()):
                self._send_json({"error": "Each agent prompt must contain text."}, status=400)
                return
            if not isinstance(repository_url, str) or len(repository_url) > 500:
                self._send_json({"error": "The GitHub repository URL is invalid."}, status=400)
                return
            repository_url = repository_url.strip()
            if repository_url:
                parse_github_repository(repository_url)
            future = asyncio.run_coroutine_threadsafe(
                run_single_agent(
                    agent_key=agent_key,
                    run_id=run_id.strip(),
                    specification=specification,
                    ui_design=ui_design,
                    prompts=prompts,
                    repository_url=repository_url or None,
                    github_token=github_token,
                ),
                self.server.event_loop,
            )
            result = future.result()
        except (ValueError, GitHubRepositoryError) as error:
            self._send_json({"error": str(error)}, status=400)
            return
        except LookupError as error:
            self._send_json({"error": str(error)}, status=404)
            return
        except TursoConfigurationError as error:
            self._send_json({"error": str(error)}, status=503)
            return
        except Exception:
            traceback.print_exc()
            self._send_json({"error": "The selected agent run failed. Check the server console for details."}, status=502)
            return
        self._send_json({key: to_json_value(value) for key, value in result.items()})

    def _save_project(self):
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > MAX_REQUEST_BYTES:
                raise ValueError("Project request body is empty or too large.")
            payload = json.loads(self.rfile.read(content_length))
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            self._send_json({"error": "Project data must be valid JSON."}, status=400)
            return
        if not isinstance(payload, dict):
            self._send_json({"error": "Project data must be a JSON object."}, status=400)
            return
        try:
            project = self._project_from_payload(payload)
            project.pop("clear_github_token", None)
            saved = TursoStore().create_project(**project)
        except (ValueError, GitHubRepositoryError) as error:
            self._send_json({"error": str(error)}, status=400)
            return
        except TursoConfigurationError as error:
            self._send_json({"error": str(error)}, status=503)
            return
        except Exception as error:
            if "UNIQUE constraint failed" in str(error):
                self._send_json({"error": "A project with that name already exists."}, status=409)
                return
            traceback.print_exc()
            self._send_json({"error": "The project could not be saved."}, status=502)
            return
        self._send_json(public_project(saved), status=201)

    def do_PUT(self):
        path = urlparse(self.path).path
        if not path.startswith("/api/projects/"):
            self._send_json({"error": "Not found."}, status=404)
            return
        try:
            project_id = int(path.rsplit("/", 1)[1])
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > MAX_REQUEST_BYTES:
                raise ValueError("Project request body is empty or too large.")
            payload = json.loads(self.rfile.read(content_length))
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            self._send_json({"error": "Project data must be valid JSON."}, status=400)
            return
        if not isinstance(payload, dict):
            self._send_json({"error": "Project data must be a JSON object."}, status=400)
            return
        try:
            project = self._project_from_payload(payload, allow_blank_token=True)
            saved = TursoStore().update_project(project_id=project_id, **project)
        except (ValueError, GitHubRepositoryError) as error:
            self._send_json({"error": str(error)}, status=400)
            return
        except LookupError as error:
            self._send_json({"error": str(error)}, status=404)
            return
        except TursoConfigurationError as error:
            self._send_json({"error": str(error)}, status=503)
            return
        except Exception as error:
            if "UNIQUE constraint failed" in str(error):
                self._send_json({"error": "A project with that name already exists."}, status=409)
                return
            traceback.print_exc()
            self._send_json({"error": "The project could not be saved."}, status=502)
            return
        self._send_json(public_project(saved))

    @staticmethod
    def _project_from_payload(payload: dict[str, Any], allow_blank_token: bool = False) -> dict[str, Any]:
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
        }

    def do_PATCH(self):
        if urlparse(self.path).path != "/api/specification":
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
        run_id = payload.get("run_id") if isinstance(payload, dict) else None
        specification = payload.get("specification") if isinstance(payload, dict) else None
        if not isinstance(run_id, str) or not run_id.strip():
            self._send_json({"error": "A run_id is required."}, status=400)
            return
        if not isinstance(specification, str) or not specification.strip():
            self._send_json({"error": "The specification cannot be empty."}, status=400)
            return
        try:
            saved = TursoStore().update_specification(
                run_id=run_id.strip(),
                specification=specification,
            )
        except LookupError as error:
            self._send_json({"error": str(error)}, status=404)
            return
        except (ValueError, TursoConfigurationError) as error:
            self._send_json({"error": str(error)}, status=400 if isinstance(error, ValueError) else 503)
            return
        except Exception:
            traceback.print_exc()
            self._send_json({"error": "The specification could not be saved."}, status=502)
            return
        self._send_json(saved)

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
