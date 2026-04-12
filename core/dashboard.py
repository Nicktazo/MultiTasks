"""Web dashboard — HTTP server for viewing run state and editing config."""

from __future__ import annotations

import html
import json
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import yaml

from .config import load_config, ConfigError, Settings
from .config_writer import (
    apply_mutation,
    _mutate_update_project,
    _mutate_delete_project,
    _mutate_upsert_task,
    _mutate_delete_task,
    _mutate_settings,
)
from .dag import get_execution_order

_MAX_BODY = 1_048_576  # 1 MB


class _DashboardHandler(BaseHTTPRequestHandler):
    """Request handler. Class variables are injected via type()."""

    state_dir: str = "state"
    template_path: str = ""
    initial_run_id: str = ""
    config_path: str = "projects.yaml"
    runner = None  # PipelineRunner, injected via type()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        params = parse_qs(parsed.query)

        if path == "/":
            self._serve_html()
        elif path == "/api/state":
            run_id = params.get("run_id", [None])[0]
            self._serve_state(run_id)
        elif path == "/api/runs":
            self._serve_runs()
        elif path == "/api/config":
            self._serve_config()
        elif path == "/api/run-status":
            self._send_json(200, self.runner.get_status())
        else:
            self._send_json(404, {"error": f"Not found: {self.path}"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        # Read body with size limit
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            length = 0
        if length > _MAX_BODY:
            self._send_json(413, {"ok": False, "config": None, "order": [], "errors": ["Request body too large"]})
            return
        raw_body = self.rfile.read(length) if length > 0 else b"{}"
        try:
            body = json.loads(raw_body)
        except json.JSONDecodeError:
            self._send_json(400, {"ok": False, "config": None, "order": [], "errors": ["Invalid JSON"]})
            return

        dispatch = {
            "/api/config/projects/update": (_mutate_update_project, lambda b: {"name": b.get("name", ""), "path": b.get("path", "")}),
            "/api/config/projects/delete": (_mutate_delete_project, lambda b: {"name": b.get("name", "")}),
            "/api/config/tasks": (_mutate_upsert_task, lambda b: {
                "project": b.get("project", ""),
                "project_path": b.get("project_path", ""),
                "id": b.get("id", ""),
                "prompt": b.get("prompt", ""),
                "tool": b.get("tool", ""),
                "depends_on": b.get("depends_on") or [],
                "review_of": b.get("review_of", ""),
            }),
            "/api/config/tasks/delete": (_mutate_delete_task, lambda b: {"project": b.get("project", ""), "id": b.get("id", "")}),
            "/api/config/settings": (_mutate_settings, lambda b: {k: v for k, v in b.items()}),
        }

        if path == "/api/validate":
            self._handle_validate()
            return

        if path == "/api/run":
            self._handle_run(body)
            return

        if path not in dispatch:
            self._send_json(404, {"ok": False, "config": None, "order": [], "errors": [f"Not found: {path}"]})
            return

        mutate_fn, args_fn = dispatch[path]
        args = args_fn(body)
        result = apply_mutation(self.config_path, mutate_fn, args)

        if result["ok"]:
            order = self._get_execution_order()
            self._send_json(200, {"ok": True, "config": result["config"], "order": order, "errors": []})
        else:
            self._send_json(400, {"ok": False, "config": None, "order": [], "errors": result["errors"]})

    def _handle_run(self, body: dict) -> None:
        mode = body.get("mode")
        if not mode:
            self._send_json(400, {"ok": False, "errors": ["Missing 'mode' field"]})
            return
        if mode not in ("run", "retry"):
            self._send_json(400, {"ok": False, "errors": [f"Invalid mode: {mode!r}. Must be 'run' or 'retry'."]})
            return
        project = body.get("project") or None
        result = self.runner.start(self.config_path, mode, project)
        if result.get("ok"):
            self._send_json(202, result)
        else:
            # Only reachable when pipeline is already running
            self._send_json(409, result)

    def _handle_validate(self) -> None:
        try:
            config = load_config(self.config_path)
            order = get_execution_order(config)
            self._send_json(200, {"ok": True, "config": None, "order": order, "errors": []})
        except ConfigError as e:
            self._send_json(400, {"ok": False, "config": None, "order": [], "errors": [str(e)]})

    def _get_execution_order(self) -> list[str]:
        try:
            config = load_config(self.config_path)
            return get_execution_order(config)
        except Exception:
            return []

    def _serve_config(self) -> None:
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except FileNotFoundError:
            defaults = Settings()
            raw = {
                "projects": {},
                "settings": {
                    "max_parallel": defaults.max_parallel,
                    "timeout": defaults.timeout,
                    "notify": defaults.notify,
                    "dashboard_port": defaults.dashboard_port,
                    "state_dir": defaults.state_dir,
                    "log_dir": defaults.log_dir,
                    "dirty_workspace": defaults.dirty_workspace,
                },
            }
        except yaml.YAMLError as e:
            self._send_json(500, {"error": f"YAML parse error: {e}"})
            return

        self._send_json(200, raw)

    def _serve_html(self) -> None:
        try:
            with open(self.template_path, "r", encoding="utf-8") as f:
                content = f.read()
        except FileNotFoundError:
            self._send_json(500, {"error": "Template not found"})
            return
        if self.initial_run_id:
            safe_id = html.escape(self.initial_run_id, quote=True)
            meta = f'<meta name="initial-run-id" content="{safe_id}">'
            content = content.replace("</head>", f"{meta}\n</head>", 1)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        encoded = content.encode("utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _serve_state(self, run_id: str | None) -> None:
        run_files = self._list_run_files()
        if not run_files:
            self._send_json(404, {"error": "No runs found"})
            return

        if run_id is None:
            target_file = run_files[0]
        else:
            target_file = f"{run_id}.json"
            if target_file not in run_files:
                self._send_json(404, {"error": f"Run not found: {run_id}"})
                return

        path = os.path.join(self.state_dir, target_file)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            self._send_json(500, {"error": str(e)})
            return

        self._send_json(200, data)

    def _serve_runs(self) -> None:
        run_files = self._list_run_files()
        runs = [f.removesuffix(".json") for f in run_files]
        self._send_json(200, runs)

    def _list_run_files(self) -> list[str]:
        if not os.path.isdir(self.state_dir):
            return []
        files = [f for f in os.listdir(self.state_dir) if f.endswith(".json")]
        files.sort(reverse=True)
        return files

    def _send_json(self, code: int, data: object) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # Silence request logs


def start_dashboard(port: int, state_dir: str = "state",
                    run_id: str | None = None,
                    config_path: str = "projects.yaml") -> bool:
    """Start the dashboard HTTP server (blocking).

    Returns True if the server ran successfully, False on startup error.
    """
    import sys
    from .run_api import PipelineRunner

    template_dir = os.path.join(os.path.dirname(__file__), os.pardir, "templates")
    template_path = os.path.normpath(os.path.join(template_dir, "dashboard.html"))

    runner = PipelineRunner()

    handler = type(
        "_Handler",
        (_DashboardHandler,),
        {"state_dir": state_dir, "template_path": template_path,
         "initial_run_id": run_id or "", "config_path": config_path,
         "runner": runner},
    )

    try:
        server = HTTPServer(("127.0.0.1", port), handler)
    except OSError as e:
        print(f"Error: cannot start dashboard on port {port}: {e}", file=sys.stderr)
        return False

    print(f"Dashboard running at http://127.0.0.1:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return True
