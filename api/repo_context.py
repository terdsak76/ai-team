"""Read the immutable artifact snapshot associated with a run's context report."""

import asyncio
import json
import traceback
from urllib.parse import parse_qs, urlparse

from api._handler import MAX_REQUEST_BYTES, ApiHandler
from github_repository import GitHubRepositoryError, parse_github_repository
from muse.database_context import DatabaseContextError, validate_database_connections
from turso_store import TursoConfigurationError, TursoStore


def load_snapshot(query: str) -> dict:
    value = parse_qs(query).get("snapshot_id", [""])[0]
    if not value.isascii() or not value.isdigit() or int(value) < 1:
        raise ValueError("A positive snapshot_id is required.")
    return TursoStore().get_repository_snapshot(int(value))


class handler(ApiHandler):
    def do_POST(self):
        send = getattr(self, "_send_json", None) or self.send_json
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_REQUEST_BYTES:
                send({"error": "Request body is empty or too large."}, status=413)
                return
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Request body must be a JSON object.")
            project_id = payload.get("project_id")
            if project_id is not None:
                if isinstance(project_id, bool) or not isinstance(project_id, int) or project_id < 1:
                    raise ValueError("The selected project is invalid.")
            repository_url = payload.get("repository_url", "")
            if not isinstance(repository_url, str) or len(repository_url) > 500:
                raise ValueError("The GitHub repository URL is invalid.")
            repository_url = repository_url.strip()
            if repository_url and project_id is None:
                parse_github_repository(repository_url)
            connections = validate_database_connections(payload.get("database_connections"))
            if project_id is None and not repository_url and not connections:
                raise ValueError("Select a saved project or provide a repository/database source before refreshing.")
        except (ValueError, TypeError, UnicodeDecodeError) as error:
            send({"error": str(error) or "Invalid refresh request."}, status=400)
            return

        if payload.get("stream") is True:
            self.start_progress_stream()
        try:
            from workflow import refresh_repo_context

            coroutine = refresh_repo_context(
                project_id=project_id, repository_url=repository_url or None,
                database_connections=connections,
                on_event=self.send_progress if payload.get("stream") is True else None,
            )
            if hasattr(getattr(self, "server", None), "event_loop"):
                result = asyncio.run_coroutine_threadsafe(coroutine, self.server.event_loop).result()
            else:
                result = asyncio.run(coroutine)
            send(result)
        except (DatabaseContextError, GitHubRepositoryError, ValueError) as error:
            send({"error": str(error)}, status=400)
        except LookupError as error:
            send({"error": str(error)}, status=404)
        except TursoConfigurationError as error:
            send({"error": str(error)}, status=503)
        except Exception:
            traceback.print_exc()
            send({"error": "Repo Context refresh failed. Check server logs for details."}, status=502)

    def do_GET(self):
        try:
            self.send_json(load_snapshot(urlparse(self.path).query))
        except ValueError as error:
            self.send_json({"error": str(error)}, status=400)
        except LookupError as error:
            self.send_json({"error": str(error)}, status=404)
        except TursoConfigurationError as error:
            self.send_json({"error": str(error)}, status=503)
        except Exception:
            traceback.print_exc()
            self.send_json({"error": "The repository snapshot could not be loaded."}, status=502)
