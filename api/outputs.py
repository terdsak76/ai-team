from urllib.parse import parse_qs, urlparse

from api._handler import ApiHandler
from turso_store import TursoConfigurationError, TursoStore


class handler(ApiHandler):
    allow_get = True

    def do_GET(self):
        if not self.allow_get:
            self.send_json({"error": "Not found."}, status=404)
            return
        params = parse_qs(urlparse(self.path).query)
        requirement_code = params.get("requirement_code", [""])[0].strip()
        project_name = params.get("project_name", [""])[0].strip()
        try:
            items = TursoStore().query_runs(
                requirement_code=requirement_code or None,
                project_name=project_name or None,
            )
        except ValueError as error:
            self.send_json({"error": str(error)}, status=400)
            return
        except TursoConfigurationError as error:
            self.send_json({"error": str(error)}, status=503)
            return
        except Exception:
            self.send_json({"error": "The saved outputs could not be loaded."}, status=502)
            return
        self.send_json({"items": items})
