"""Web dashboard — HTTP server for viewing run state and editing config."""

from __future__ import annotations

import html
import json
import os
import re
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import yaml

from .config import load_config, ConfigError, Settings
from .config_writer import (
    apply_mutation,
    _mutate_update_project,
    _mutate_delete_project,
    _mutate_upsert_task,
    _mutate_delete_task,
    _mutate_toggle_done,
    _mutate_settings,
)
from .dag import get_execution_order
from .chat import (
    WorkspaceStore, build_task_list, build_run_summary,
    chat_reply, generate_tasks, _extract_log_result,
    send_to_whatsapp,
)
from .reply_token import ReplyTokenStore

_MAX_BODY = 1_048_576  # 1 MB


class _DashboardHandler(BaseHTTPRequestHandler):
    """Request handler. Class variables are injected via type()."""

    state_dir: str = "state"
    template_path: str = ""
    reply_template_path: str = ""
    initial_run_id: str = ""
    config_path: str = "projects.yaml"
    runner = None  # PipelineRunner, injected via type()
    workspace_store: WorkspaceStore = None  # type: ignore
    reply_token_store: ReplyTokenStore = None  # type: ignore

    def do_GET(self) -> None:
        try:
            self._route_get()
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._send_json(500, {"error": f"Internal error: {type(e).__name__}: {str(e)[:200]}"})

    def _route_get(self) -> None:
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
        elif path == "/api/run-detail":
            run_id = params.get("run_id", [None])[0]
            self._serve_run_detail(run_id)
        elif path == "/api/latest-run-detail":
            self._serve_run_detail(None)
        elif path == "/api/log":
            log_path = params.get("path", [None])[0]
            self._serve_log(log_path)
        elif path == "/api/events":
            self._serve_events()
        elif path == "/api/workspaces":
            self._serve_workspaces()
        elif path == "/api/workspace":
            ws_name = params.get("name", [None])[0]
            self._serve_workspace(ws_name)
        else:
            m = re.match(r"^/reply/([A-Za-z0-9_-]+)$", path)
            if m:
                self._serve_reply_page(m.group(1))
            else:
                self._send_json(404, {"error": f"Not found: {self.path}"})

    def do_POST(self) -> None:
        try:
            self._route_post()
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._send_json(500, {"ok": False, "error": f"Internal error: {type(e).__name__}: {str(e)[:200]}"})

    def _route_post(self) -> None:
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
            "/api/config/tasks/toggle-done": (_mutate_toggle_done, lambda b: {"project": b.get("project", ""), "id": b.get("id", "")}),
            "/api/config/settings": (_mutate_settings, lambda b: {k: v for k, v in b.items()}),
        }

        m = re.match(r"^/api/reply/([A-Za-z0-9_-]+)$", path)
        if m:
            self._handle_reply_submit(m.group(1), body)
            return

        if path == "/api/validate":
            self._handle_validate()
            return

        if path == "/api/run":
            self._handle_run(body)
            return

        if path == "/api/workspaces" and self.command == "POST":
            self._handle_create_workspace(body)
            return

        if path == "/api/workspace/chat":
            self._handle_chat(body)
            return

        if path == "/api/workspace/generate-tasks":
            self._handle_generate(body)
            return

        if path == "/api/workspace/delete":
            self._handle_delete_workspace(body)
            return

        if path == "/api/workspace/allowed-tools":
            self._handle_update_allowed_tools(body)
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

    # ---- Workspace / Chat endpoints ----

    def _serve_workspaces(self) -> None:
        self._send_json(200, self.workspace_store.list())

    def _serve_workspace(self, name: str | None) -> None:
        if not name:
            self._send_json(400, {"error": "Missing 'name' parameter"})
            return
        ws = self.workspace_store.get(name)
        if ws is None:
            self._send_json(404, {"error": f"Workspace not found: {name}"})
            return
        self._send_json(200, ws)

    def _handle_create_workspace(self, body: dict) -> None:
        name = body.get("name", "").strip()
        path = body.get("path", "").strip()
        if not name:
            self._send_json(400, {"ok": False, "error": "Name is required"})
            return
        try:
            ws = self.workspace_store.create(name, path, self.config_path)
            self._send_json(201, {"ok": True, "workspace": ws})
        except ValueError as e:
            self._send_json(400, {"ok": False, "error": str(e)})

    def _handle_delete_workspace(self, body: dict) -> None:
        name = body.get("name", "").strip()
        if not name:
            self._send_json(400, {"ok": False, "error": "Name is required"})
            return
        deleted = self.workspace_store.delete(name)
        if deleted:
            self._send_json(200, {"ok": True})
        else:
            self._send_json(404, {"ok": False, "error": "Workspace not found"})

    def _handle_update_allowed_tools(self, body: dict) -> None:
        name = body.get("name", "").strip()
        tools = body.get("tools")
        if not name or not isinstance(tools, list):
            self._send_json(400, {"ok": False, "error": "name and tools[] required"})
            return
        # Sanitize: keep only non-empty strings
        tools = [str(t).strip() for t in tools if str(t).strip()]
        self.workspace_store.update_allowed_tools(name, tools)
        self._send_json(200, {"ok": True, "allowed_tools": tools})

    def _handle_chat(self, body: dict) -> None:
        name = body.get("name", "").strip()
        message = body.get("message", "").strip()
        if not name or not message:
            self._send_json(400, {"ok": False, "error": "name and message required"})
            return
        ws = self.workspace_store.get(name)
        if ws is None:
            self._send_json(404, {"ok": False, "error": f"Workspace not found: {name}"})
            return

        task_list = build_task_list(self.config_path, name)
        run_summary = build_run_summary(self.state_dir, name)
        result = chat_reply(ws, message, task_list, run_summary,
                            workspace_store=self.workspace_store)

        if result["ok"]:
            turn = self.workspace_store.save_turn(
                name, message, result["reply"], result["tasks"]
            )
            # WhatsApp push (only if mobile_mode + public_base_url configured)
            base_url = self._get_setting("public_base_url", "")
            mobile = self._get_setting("mobile_mode", "") == "True"
            if mobile and base_url and turn:
                token = self.reply_token_store.create(
                    workspace=name,
                    assistant_msg_id=turn["assistant_id"],
                    assistant_reply=result["reply"],
                )
                reply_link = f"{base_url}/reply/{token}"
                wa_text = f"[MT:{name}]\n\n{result['reply']}\n\nReply: {reply_link}"
                threading.Thread(
                    target=send_to_whatsapp, args=(wa_text,), daemon=True
                ).start()
        self._send_json(200, result)

    def _handle_generate(self, body: dict) -> None:
        name = body.get("name", "").strip()
        message = body.get("message", "").strip()
        if not name or not message:
            self._send_json(400, {"ok": False, "error": "name and message required"})
            return
        ws = self.workspace_store.get(name)
        if ws is None:
            self._send_json(404, {"ok": False, "error": f"Workspace not found: {name}"})
            return

        result = generate_tasks(name, message, self.config_path)
        self._send_json(200, result)

    # ---- Reply link endpoints ----

    def _serve_reply_page(self, token: str) -> None:
        """GET /reply/<token> — render the reply form page."""
        result = self.reply_token_store.peek(token, self.workspace_store)
        try:
            with open(self.reply_template_path, "r", encoding="utf-8") as f:
                tpl = f.read()
        except FileNotFoundError:
            self._send_json(500, {"error": "Reply template not found"})
            return

        if not result["ok"]:
            reason = result["reason"]
            messages = {
                "not_found": ("Not Found", "This reply link does not exist."),
                "already_used": ("Already Used", "This reply link has already been used."),
                "expired": ("Expired", "This reply link has expired (15 min)."),
                "conversation_moved": ("Conversation Moved",
                                       "The conversation has moved on. Check WhatsApp for the latest link."),
                "processing": ("Processing", "A reply is being processed. Please wait and refresh."),
            }
            title, msg = messages.get(reason, ("Error", reason))
            content = (
                f'<div class="status-page">'
                f'<h2>{html.escape(title)}</h2>'
                f'<p>{html.escape(msg)}</p></div>'
            )
            page = tpl.replace("{{WORKSPACE}}", "Reply").replace("{{CONTENT}}", content)
            status = 404 if reason == "not_found" else 410
            self._send_html(status, page, cache_control="no-store")
            return

        entry = result["entry"]
        ws_name = html.escape(entry["workspace"])
        reply_text = html.escape(entry["assistant_reply"])
        content = (
            f'<div class="header">[MT:{ws_name}]</div>'
            f'<div class="reply-box">{reply_text}</div>'
            f'<form id="reply-form" data-token="{html.escape(token)}">'
            f'<div class="input-area">'
            f'<textarea id="reply-input" placeholder="Type your reply..." '
            f'autofocus required></textarea></div>'
            f'<button type="submit" id="send-btn" class="btn btn-primary">Send</button>'
            f'<div id="error-area" class="error-msg"></div>'
            f'</form>'
            f'<div id="result-area"></div>'
        )
        page = tpl.replace("{{WORKSPACE}}", ws_name).replace("{{CONTENT}}", content)
        self._send_html(200, page, cache_control="no-store")

    def _handle_reply_submit(self, token: str, body: dict) -> None:
        """POST /api/reply/<token> — submit a reply via one-time link."""
        message = body.get("message", "").strip()
        if not message:
            self._send_json(400, {"ok": False, "error": "message required"},
                            cache_control="no-store")
            return

        store = self.reply_token_store
        result = store.reserve(token, self.workspace_store)
        if not result["ok"]:
            status = 404 if result["reason"] == "not_found" else 410
            self._send_json(status, {"ok": False, "reason": result["reason"]},
                            cache_control="no-store")
            return

        entry = result["entry"]
        workspace_name = entry["workspace"]
        ws = self.workspace_store.get(workspace_name)
        if not ws:
            store.release(token)
            self._send_json(404, {"ok": False, "error": "Workspace not found"},
                            cache_control="no-store")
            return

        # Call Claude
        task_list = build_task_list(self.config_path, workspace_name)
        run_summary = build_run_summary(self.state_dir, workspace_name)
        chat_result = chat_reply(ws, message, task_list, run_summary,
                                 workspace_store=self.workspace_store)

        if not chat_result["ok"]:
            store.release(token)  # failure → release, user can retry
            self._send_json(200, {"ok": False, "error": chat_result["error"],
                                  "retryable": True},
                            cache_control="no-store")
            return

        # Success path — finalize only after save_turn succeeds
        turn = self.workspace_store.save_turn(
            workspace_name, message, chat_result["reply"], chat_result["tasks"]
        )
        if not turn:
            store.release(token)
            self._send_json(200, {"ok": False, "error": "Failed to save turn",
                                  "retryable": True},
                            cache_control="no-store")
            return
        store.finalize(token)

        resp: dict = {"ok": True, "reply": chat_result["reply"]}

        # Chain: generate new token + push WhatsApp
        base_url = self._get_setting("public_base_url", "")
        mobile = self._get_setting("mobile_mode", "") == "True"
        if mobile and base_url and turn:
            new_token = store.create(
                workspace=workspace_name,
                assistant_msg_id=turn["assistant_id"],
                assistant_reply=chat_result["reply"],
            )
            new_link = f"{base_url}/reply/{new_token}"
            resp["new_reply_url"] = new_link
            wa_text = (f"[MT:{workspace_name}]\n\n"
                       f"{chat_result['reply']}\n\nReply: {new_link}")
            threading.Thread(
                target=send_to_whatsapp, args=(wa_text,), daemon=True
            ).start()

        self._send_json(200, resp, cache_control="no-store")

    def _get_setting(self, key: str, default: str = "") -> str:
        """Read a single setting value from the YAML config."""
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except (FileNotFoundError, yaml.YAMLError):
            return default
        return str((raw.get("settings") or {}).get(key, default))

    def _send_html(self, code: int, content: str,
                   cache_control: str | None = None) -> None:
        """Send an HTML response."""
        encoded = content.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        if cache_control:
            self.send_header("Cache-Control", cache_control)
        self.end_headers()
        self.wfile.write(encoded)

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
                    "public_base_url": defaults.public_base_url,
                    "listen_address": defaults.listen_address,
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

    def _serve_run_detail(self, run_id: str | None) -> None:
        run_files = self._list_run_files()
        if not run_files:
            self._send_json(404, {"ok": False, "run": None, "events": [], "errors": ["No runs found"]})
            return

        if run_id is None:
            target_file = run_files[0]
            run_id = target_file.removesuffix(".json")
        else:
            target_file = f"{run_id}.json"
            if target_file not in run_files:
                self._send_json(404, {"ok": False, "run": None, "events": [],
                                      "errors": [f"Run not found: {run_id}"]})
                return

        path = os.path.join(self.state_dir, target_file)
        try:
            with open(path, "r", encoding="utf-8") as f:
                run_data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            self._send_json(500, {"ok": False, "run": None, "events": [],
                                  "errors": [f"Failed to load run state: {e}"]})
            return

        events = self._load_event_log(run_id)
        self._send_json(200, {"ok": True, "run": run_data, "events": events, "errors": []})

    def _serve_log(self, log_path: str | None) -> None:
        import re
        _MAX_RESULT = 102_400  # 100 KB cap on result text
        _MAX_RAW = 10_485_760  # 10 MB hard cap on raw file read
        if not log_path:
            self._send_json(400, {"ok": False, "error": "Missing 'path' parameter"})
            return

        # Resolve relative to the project root (parent of core/)
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        candidate = os.path.join(project_root, log_path)

        # Security: realpath resolves symlinks, preventing symlink escape
        resolved = os.path.realpath(candidate)
        logs_dir = os.path.realpath(os.path.join(project_root, "logs"))
        try:
            common = os.path.commonpath([resolved, logs_dir])
        except ValueError:
            # Different drives on Windows
            common = ""
        if common != logs_dir:
            self._send_json(403, {"ok": False, "error": "Access denied"})
            return

        if not os.path.isfile(resolved):
            self._send_json(404, {"ok": False, "error": "Log file not found"})
            return

        try:
            with open(resolved, "r", encoding="utf-8", errors="replace") as f:
                raw = f.read(_MAX_RAW)
        except OSError as e:
            self._send_json(500, {"ok": False, "error": str(e)})
            return

        # Try to parse Claude CLI output format and return structured data
        parsed = self._parse_ai_log(raw, _MAX_RESULT)
        if parsed:
            self._send_json(200, parsed)
        else:
            # Fallback: return raw content with truncation
            truncated = len(raw) > _MAX_RESULT
            if truncated:
                raw = raw[:_MAX_RESULT]
            self._send_json(200, {"ok": True, "format": "raw",
                                  "content": raw, "truncated": truncated})

    @staticmethod
    def _parse_ai_log(raw: str, max_result: int) -> dict | None:
        """Parse Claude or Codex CLI log format into structured response."""
        import re
        m = re.search(r"=== STDOUT ===\s*\n(.*?)(?:\n\s*=== STDERR ===|$)",
                      raw, re.DOTALL)
        if not m:
            return None
        json_str = m.group(1).strip()

        # 1) Try Claude single-JSON format
        try:
            data = json.loads(json_str)
        except (json.JSONDecodeError, ValueError):
            data = None

        if isinstance(data, dict) and data.get("type") == "result":
            result_text = data.get("result", "")
            truncated = len(result_text) > max_result
            if truncated:
                result_text = result_text[:max_result]

            # Extract stderr
            sm = re.search(r"=== STDERR ===\s*\n(.*)", raw, re.DOTALL)
            stderr = sm.group(1).strip() if sm else ""
            if stderr == "(empty)":
                stderr = ""

            meta: dict = {
                "is_ok": data.get("subtype") == "success" and not data.get("is_error"),
            }
            if data.get("duration_ms") is not None:
                meta["duration_s"] = round(data["duration_ms"] / 1000)
            if data.get("num_turns") is not None:
                meta["num_turns"] = data["num_turns"]
            if data.get("total_cost_usd") is not None:
                meta["cost_usd"] = round(data["total_cost_usd"], 4)
            if data.get("modelUsage"):
                meta["models"] = list(data["modelUsage"].keys())

            return {"ok": True, "format": "claude", "meta": meta,
                    "result": result_text, "stderr": stderr,
                    "truncated": truncated}

        # 2) Try codex streaming format (one JSON per line)
        agent_messages = []
        usage = {}
        for line in json_str.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if obj.get("type") == "item.completed":
                item = obj.get("item", {})
                if item.get("type") == "agent_message" and item.get("text"):
                    agent_messages.append(item["text"])
            elif obj.get("type") == "turn.completed":
                usage = obj.get("usage", {})

        if not agent_messages:
            return None

        result_text = agent_messages[-1]
        truncated = len(result_text) > max_result
        if truncated:
            result_text = result_text[:max_result]

        # Extract stderr
        sm = re.search(r"=== STDERR ===\s*\n(.*?)(?:\n\s*=== RESULT ===|$)",
                       raw, re.DOTALL)
        stderr = sm.group(1).strip() if sm else ""
        if stderr == "(empty)":
            stderr = ""

        # is_ok: derive from exit code in === RESULT === section only
        result_section = re.search(r"=== RESULT ===\s*\n(.*)",
                                   raw, re.DOTALL)
        result_block = result_section.group(1) if result_section else ""
        exit_m = re.search(r"^Exit code:\s*(\S+)", result_block,
                           re.MULTILINE)
        is_ok = exit_m.group(1) == "0" if exit_m else True

        meta = {"is_ok": is_ok}
        if usage.get("input_tokens"):
            meta["input_tokens"] = usage["input_tokens"]
        if usage.get("output_tokens"):
            meta["output_tokens"] = usage["output_tokens"]

        return {"ok": True, "format": "codex", "meta": meta,
                "result": result_text, "stderr": stderr,
                "truncated": truncated}

    def _load_event_log(self, run_id: str) -> list[dict]:
        event_path = os.path.join(self.state_dir, f"{run_id}.events.jsonl")
        if not os.path.isfile(event_path):
            return []
        events = []
        try:
            with open(event_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return []
        return events

    def _serve_events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        last_status = None
        last_state_key: tuple[str, float] = ("", 0.0)

        try:
            while True:
                current = self.runner.get_status()
                if current != last_status:
                    last_status = current
                    self._write_sse("run-status", current)

                state_key = self._get_latest_state_key()
                if state_key != last_state_key:
                    last_state_key = state_key
                    self._write_sse("state-updated", {})

                time.sleep(1)
        except (BrokenPipeError, ConnectionError, OSError):
            pass  # client disconnected

    def _write_sse(self, event: str, data: dict) -> None:
        payload = json.dumps(data, ensure_ascii=False)
        self.wfile.write(f"event: {event}\ndata: {payload}\n\n".encode())
        self.wfile.flush()

    def _get_latest_state_key(self) -> tuple[str, float]:
        """Return (filename, mtime) of the latest state file."""
        files = self._list_run_files()
        if not files:
            return ("", 0.0)
        filename = files[0]
        path = os.path.join(self.state_dir, filename)
        try:
            return (filename, os.path.getmtime(path))
        except OSError:
            return (filename, 0.0)

    def _send_json(self, code: int, data: object,
                   cache_control: str | None = None) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if cache_control:
            self.send_header("Cache-Control", cache_control)
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
    reply_template_path = os.path.normpath(os.path.join(template_dir, "reply.html"))

    runner = PipelineRunner()
    workspace_store = WorkspaceStore(
        os.path.join(os.path.dirname(config_path) or ".", "workspaces")
    )
    reply_token_store = ReplyTokenStore(
        path=os.path.join(state_dir, "reply_tokens.json")
    )

    _MAX_SYSTEM_MSG = 3000

    def _on_run_complete(state, error, project):
        """Fan out run results to workspace chat as system messages."""
        if state is None:
            # Runner-level failure — no RunState, notify project only
            if project:
                workspace_store.save_system_message(
                    project,
                    f"Pipeline failed: {error or 'unknown error'}",
                    data={"error": error},
                )
            return

        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        # Group tasks by workspace (task_id format is "project:task")
        ws_tasks: dict[str, list] = {}
        for tid, ts in state.tasks.items():
            ws_name = tid.split(":")[0] if ":" in tid else None
            if ws_name:
                ws_tasks.setdefault(ws_name, []).append((tid, ts))

        for ws_name, task_list in ws_tasks.items():
            ws = workspace_store.get(ws_name)
            if not ws:
                continue

            tasks_data = []
            for tid, ts in task_list:
                task_info = {
                    "task_id": tid,
                    "status": ts.status,
                    "duration_s": ts.duration_s,
                    "error": (ts.error or "")[:200],
                }
                if ts.status == "done" and ts.log_file:
                    result = _extract_log_result(project_root, ts.log_file)
                    task_info["result"] = result[:500] if result else ""
                tasks_data.append(task_info)

            # Human-readable summary
            done = sum(1 for td in tasks_data if td["status"] == "done")
            failed = sum(1 for td in tasks_data if td["status"] == "failed")
            skipped = sum(1 for td in tasks_data if td["status"] == "skipped")
            parts = []
            if done:
                parts.append(f"{done} done")
            if failed:
                parts.append(f"{failed} failed")
            if skipped:
                parts.append(f"{skipped} skipped")
            summary = f"Run {state.run_id}: {', '.join(parts)}"

            for td in tasks_data:
                summary += f"\n\n{td['task_id']}: {td['status']}"
                if td.get("duration_s") is not None:
                    summary += f" ({td['duration_s']}s)"
                if td.get("error"):
                    summary += f"\nError: {td['error']}"
                elif td.get("result"):
                    summary += f"\n{td['result'][:300]}"

            summary = summary[:_MAX_SYSTEM_MSG]
            workspace_store.save_system_message(
                ws_name, summary,
                data={"run_id": state.run_id, "tasks": tasks_data},
            )

    runner.on_complete = _on_run_complete

    handler = type(
        "_Handler",
        (_DashboardHandler,),
        {"state_dir": state_dir, "template_path": template_path,
         "reply_template_path": reply_template_path,
         "initial_run_id": run_id or "", "config_path": config_path,
         "runner": runner, "workspace_store": workspace_store,
         "reply_token_store": reply_token_store},
    )

    # Read listen_address from config (default 127.0.0.1)
    listen_address = "127.0.0.1"
    try:
        cfg = load_config(config_path)
        listen_address = cfg.settings.listen_address
    except ConfigError:
        pass

    try:
        server = ThreadingHTTPServer((listen_address, port), handler)
    except OSError as e:
        print(f"Error: cannot start dashboard on {listen_address}:{port}: {e}", file=sys.stderr)
        return False

    print(f"Dashboard running at http://{listen_address}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return True
