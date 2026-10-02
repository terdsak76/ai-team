"""Optional NDJSON progress transport shared by local and serverless handlers."""

import json


def json_value(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raise TypeError(f"Cannot serialize {type(value).__name__}")


class ProgressStreamMixin:
    def start_progress_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self._progress_started = True

    def send_progress(self, event):
        if getattr(self, "_progress_disconnected", False):
            return
        content = (json.dumps(event, ensure_ascii=False, default=json_value) + "\n").encode("utf-8")
        try:
            self.wfile.write(content)
            self.wfile.flush()
        except OSError:
            # A disconnected browser must not interrupt persistence of the run.
            self._progress_disconnected = True

    def send_progress_result(self, payload, status=200):
        if not getattr(self, "_progress_started", False):
            return False
        if status >= 400:
            self.send_progress({"type": "error", "error": payload.get("error", "The workflow failed.")})
        else:
            self.send_progress({"type": "result", "data": payload})
        return True
