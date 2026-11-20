"""Small localhost web interface for the existing coordinator."""
from __future__ import annotations

import http.client
import json
import os
import re
import secrets
import socket
import threading
import webbrowser
from contextlib import closing, contextmanager
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, urlsplit

from .chat import ChatBusy, ChatConflict, ChatService, _request_id, _text
from .chat_output import BRIEF_FIELDS, coding_prompt, draft, export
from .chat_store import ChatStore
from .config import available_agents, load, team_dir
from .coordinator import Coordinator
from .quota import collect as collect_quota
from .roles import ROLE_LABELS, validate_roles


DEFAULT_PORT = 8765
PORT_ATTEMPTS = 10
TASK_PATH = re.compile(r"^/api/tasks/([a-f0-9]{12})(?:/(diff|resume|retry|stop|guide|message))?$")
SESSION_PATH = re.compile(r"^/api/v1/sessions/([a-f0-9]{32})(/messages)?$")
SESSION_LIVE_PATH = re.compile(r"^/api/v1/sessions/([a-f0-9]{32})/(events|cancel)$")
SESSION_OUTPUT_PATH = re.compile(r"^/api/v1/sessions/([a-f0-9]{32})/(branch|export|handoff)$")
LINKED_OUTPUT_PATH = re.compile(r"^/api/v1/sessions/([a-f0-9]{32})/artifacts/([a-f0-9]{12})$")
RUN_ACTION_PATH = re.compile(r"^/api/v1/sessions/([a-f0-9]{32})/runs/([a-f0-9]{32})/(pause|stop|cancel|next|retry|synthesize|choose|expand|promote)$")
MODELS_PATH = "/api/v1/models"
SESSIONS_PATH = "/api/v1/sessions"
TEAMS_PATH = "/api/v1/teams"
PREVIEW_PATH = "/chat/preview"
PREVIEW_ASSETS = f"{PREVIEW_PATH}/assets/"
ASSET_NAME = re.compile(r"[A-Za-z0-9_-]+\.(js|css)")
DEFAULT_MODE_MARKER = "__AI_TEAM_DEFAULT_MODE__"
DEFAULT_PAGES = {"/", "/chat", "/chat/"}
CLASSIC_PAGES = {"/classic", "/classic/chat"}
PAGE_POLICY = "default-src 'none'; connect-src 'self'; base-uri 'none'; form-action 'none'"
MAX_BODY_BYTES = 32_768


class WebApp:
    def __init__(self, root: Path, token: str | None = None):
        self.root = root.resolve()
        self.chat = ChatService(self.root)
        self.token = token or secrets.token_urlsafe(32)
        self.active: set[str] = set()
        self.activity: dict[str, dict[str, str]] = {}
        self.lock = threading.Lock()

    @contextmanager
    def coordinator(self) -> Iterator[Coordinator]:
        coordinator = Coordinator(self.root, load(self.root), activity=self._activity)
        try:
            yield coordinator
        finally:
            coordinator.store.close()

    def overview(self) -> dict[str, Any]:
        config = load(self.root)
        with self.coordinator() as coordinator:
            tasks = coordinator.store.list_tasks()
            usage = coordinator.store.last_agent_usage()
            quotas = coordinator.store.agent_quotas()
        with self.lock:
            active = set(self.active)
            activity = dict(self.activity)
        return {
            "project": {"name": self.root.name, "path": str(self.root)},
            "agents": available_agents(config),
            "default_roles": {name: settings["role"] for name, settings in config["agents"].items() if settings["enabled"]},
            "role_options": ROLE_LABELS,
            "activity": activity,
            "usage": usage,
            "quotas": quotas,
            "tasks": [{"id": task["id"], "title": task["title"], "status": task["status"],
                       "turn_count": task["turn_count"], "created_at": task["created_at"],
                       "active": task["id"] in active} for task in tasks],
        }

    def refresh_quotas(self) -> None:
        config = load(self.root)
        for agent, settings in config["agents"].items():
            if not settings["enabled"]:
                continue
            try:
                snapshot = collect_quota(agent, settings["command"], self.root)
                with self.coordinator() as coordinator:
                    coordinator.store.save_quota(agent, snapshot)
            except (OSError, ValueError, TimeoutError):
                continue

    def task(self, task_id: str) -> dict[str, Any]:
        with self.coordinator() as coordinator:
            task = coordinator.store.get_task(task_id)
            task["budget_summary"] = coordinator.store.budget_summary(task)
            messages = coordinator.store.messages(task_id)
            invocation = coordinator.store.db.execute(
                "SELECT exit_code FROM invocations WHERE task_id=? ORDER BY rowid DESC LIMIT 1", (task_id,)
            ).fetchone()
            task["can_retry"] = (task["status"] in {"failed", "awaiting_human"}
                                 and ((task["git_state"].get("setup_failure") and bool(task["queue"]))
                                      or (invocation is None and bool(task["queue"]))
                                      or (invocation is not None and invocation["exit_code"] != 0)))
        with self.lock:
            task["active"] = task_id in self.active
            task["activity"] = self.activity.get(task_id)
        with closing(ChatStore(self.chat.path)) as chat:
            source = chat.handoff_source(task_id)
        return {"task": task, "messages": messages, "source": source}

    def diff(self, task_id: str) -> dict[str, Any]:
        with self.coordinator() as coordinator:
            task = coordinator.store.get_task(task_id)
            base = task["git_state"].get("base", "HEAD")
            changes = {}
            for agent in coordinator.config["agents"]:
                path = coordinator.git.path(task_id, agent)
                if path.exists():
                    diff = coordinator.git.diff(path, base)
                    changes[agent] = {"files": coordinator.git.changed_files(path, base),
                                      "diff": diff[:200_000], "truncated": len(diff) > 200_000}
        return {"changes": changes}

    def create(self, prompt: str, roles: dict[str, str] | None = None, recipient: str = "all",
               *, handoff: dict | None = None) -> str:
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 20_000:
            raise ValueError("Enter a task of at most 20,000 characters")
        config = load(self.root)
        roles = validate_roles(roles, config["agents"])
        agents = available_agents(config)
        missing = [name for name, found in agents.items() if not found and recipient in {"all", name}]
        if missing:
            raise ValueError(f"Required CLIs missing: {', '.join(missing)}. Set their command paths in .ai-team/config.yaml.")
        with self.coordinator() as coordinator:
            task = coordinator.create(prompt, roles, recipient, handoff=handoff)
        if not task.get("replayed"):
            self._launch(task["id"], "resume")
        return task["id"]

    def handoff_draft(self, session_id: str) -> dict:
        view = self.chat.get(session_id)
        config = load(self.root)
        agents = available_agents(config)
        ready = [name for name, available in agents.items() if available]
        primary = config["git"]["primary_implementation_agent"]
        roles = {}
        for name in agents:
            role = config["agents"][name]["role"].casefold()
            roles[name] = "implementation" if name == primary and not config["agents"][name]["read_only"] else (
                "qa" if "qa" in role else "q&a" if role == "q&a" else "review")
        return {"brief": draft(view), "repository": str(self.root), "agents": agents, "roles": roles,
                "role_options": ROLE_LABELS, "recipient": "all" if len(ready) == len(agents) else next(iter(ready), "all")}

    def handoff(self, session_id: str, body: dict) -> str:
        if body.get("confirmed") is not True:
            raise ChatConflict("Review the handoff and confirm starting the Code Mode task.")
        if body.get("repository") != str(self.root):
            raise ChatConflict("Review the repository shown by the current Code Mode dashboard.")
        request_id = _request_id(body)
        fields = body.get("brief")
        if not isinstance(fields, dict):
            raise ValueError("Review and complete the coding brief.")
        brief = {key: _text(fields.get(key, ""), label, 4500, blank=key != "objective")
                 for key, label in BRIEF_FIELDS.items()}
        prompt = coding_prompt(brief)
        if len(prompt.encode("utf-8")) > 20_000:
            raise ValueError("Shorten the coding brief to 20,000 UTF-8 bytes.")
        roles = validate_roles(body.get("roles"), load(self.root)["agents"])
        recipient = body.get("recipient", "all")
        if not isinstance(recipient, str):
            raise ValueError("Choose one agent or the whole team.")
        with closing(ChatStore(self.chat.path)) as store:
            store.session(session_id)
        payload = {"repository": str(self.root), "brief": brief, "roles": roles, "recipient": recipient}
        handoff = {"session_id": session_id, "request_id": request_id, "payload": json.dumps(payload, sort_keys=True)}
        task_id = self.create(prompt, roles, recipient, handoff=handoff)
        self.chat.live.changed(session_id)
        return task_id

    def action(self, task_id: str, action: str) -> None:
        if action == "stop":
            with self.coordinator() as coordinator:
                coordinator.stop(task_id)
            with self.lock:
                self.activity.pop(task_id, None)
            return
        with self.coordinator() as coordinator:
            task = coordinator.store.get_task(task_id)
            if action == "retry":
                invocation = coordinator.store.db.execute(
                    "SELECT exit_code FROM invocations WHERE task_id=? ORDER BY rowid DESC LIMIT 1", (task_id,)
                ).fetchone()
                if (task["status"] not in {"failed", "awaiting_human"}
                        or (not task["git_state"].get("setup_failure")
                            and ((invocation is None and not task["queue"])
                                 or (invocation is not None and invocation["exit_code"] == 0)))):
                    raise ValueError("Task has no failed CLI turn to retry")
            if action == "resume" and task["status"] != "running":
                raise ValueError("Only a running task can be resumed")
        self._launch(task_id, action)

    def guide(self, task_id: str, message: str, recipient: str) -> None:
        with self.lock:
            if task_id in self.active:
                raise ValueError("This task already has an active run")
        with self.coordinator() as coordinator:
            coordinator.guide(task_id, message, recipient)
        self._launch(task_id, "resume")

    def message(self, task_id: str, message: str, recipient: str) -> None:
        with self.lock:
            if task_id in self.active:
                raise ValueError("Wait for the current agent turn before messaging")
        with self.coordinator() as coordinator:
            coordinator.send_message(task_id, message, recipient)
        self._launch(task_id, "resume")

    def _launch(self, task_id: str, action: str) -> None:
        with self.lock:
            if task_id in self.active:
                raise ValueError("This task already has an active run")
            self.active.add(task_id)
            self.activity[task_id] = {"agent": "", "stage": "starting"}
        thread = threading.Thread(target=self._run, args=(task_id, action), daemon=True,
                                  name=f"ai-team-{task_id}")
        thread.start()

    def _activity(self, task_id: str, agent: str, stage: str) -> None:
        with self.lock:
            if stage == "idle":
                self.activity.pop(task_id, None)
            else:
                self.activity[task_id] = {"agent": agent, "stage": stage}

    def _run(self, task_id: str, action: str) -> None:
        try:
            with self.coordinator() as coordinator:
                if action == "retry":
                    coordinator.retry_failed(task_id)
                else:
                    coordinator.resume(task_id)
        except Exception as error:
            with self.coordinator() as coordinator:
                task = coordinator.store.get_task(task_id)
                if task["status"] != "stopped":
                    task["status"] = "failed"
                    task["git_state"]["setup_failure"] = True
                    coordinator.store.save_task(task)
                coordinator.store.add_message(task_id, "system", "human", f"Run failed: {error}")
        finally:
            with self.lock:
                self.active.discard(task_id)
                self.activity.pop(task_id, None)


class LocalServer(ThreadingHTTPServer):
    # Windows SO_REUSEADDR lets a second process bind a port that is already listening,
    # which would silently split requests between two dashboards.
    allow_reuse_address = os.name != "nt"

    def server_bind(self) -> None:
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def server_close(self) -> None:
        try:
            if hasattr(self, "app"):
                self.app.chat.close()
        finally:
            super().server_close()


def make_server(root: Path, port: int = DEFAULT_PORT, token: str | None = None, *,
                chat_only: bool = False) -> LocalServer:
    app = WebApp(root, token)
    web = files("ai_team").joinpath("web")
    preview = web.joinpath("modern")
    preview_index = preview.joinpath("index.html")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def _local_host(self) -> bool:
            expected = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            return self.headers.get("Host", "") in expected

        def _authorized(self) -> bool:
            return self._local_host() and self.headers.get("X-AI-Team-Token") == app.token

        def _local_origin(self) -> bool:
            origin = self.headers.get("Origin")
            return not origin or origin in {f"http://127.0.0.1:{self.server.server_port}",
                                            f"http://localhost:{self.server.server_port}"}

        def _json(self, status: int, body: dict[str, Any]) -> None:
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self._web_content(encoded, "application/json; charset=utf-8", status=status)

        def _error(self, status: int, message: str) -> None:
            self._json(status, {"error": message})

        def _web_content(self, encoded: bytes, media_type: str, *, policy: str | None = None,
                         cache: str = "no-store", status: int = HTTPStatus.OK, filename: str | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", media_type)
            if policy:
                self.send_header("Content-Security-Policy", policy)
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(encoded)))
            try:
                self.end_headers()
                self.wfile.write(encoded)
            except (ConnectionError, TimeoutError):
                self.close_connection = True

        def _frontend_asset(self, path: str) -> None:
            name = path.removeprefix(PREVIEW_ASSETS)
            match = ASSET_NAME.fullmatch(name)
            if match and preview_index.is_file():
                asset = preview.joinpath("assets", name)
                if asset.is_file():
                    media_type = {"js": "text/javascript", "css": "text/css"}[match[1]]
                    self._web_content(asset.read_bytes(), f"{media_type}; charset=utf-8",
                                      cache="private, max-age=31536000, immutable")
                    return
            self._error(HTTPStatus.NOT_FOUND, "Dashboard asset not found")

        def _frontend_page(self, template: str, mode: str) -> None:
            page = (template.replace("__AI_TEAM_CODE_MODE__", "disabled" if chat_only else "enabled")
                    .replace(DEFAULT_MODE_MARKER, mode)
                    .replace("__AI_TEAM_TOKEN__", escape(app.token, quote=True)))
            self._web_content(page.encode("utf-8"), "text/html; charset=utf-8",
                              policy=f"{PAGE_POLICY}; style-src 'self'; script-src 'self'; frame-ancestors 'none'")

        def do_GET(self) -> None:
            url = urlsplit(self.path)
            path = url.path
            is_preview = path in {PREVIEW_PATH, f"{PREVIEW_PATH}/"}
            is_asset = path.startswith(PREVIEW_ASSETS)
            if (is_preview or is_asset or path in DEFAULT_PAGES | CLASSIC_PAGES | {"/api/ping"}) and not self._local_host():
                self._error(HTTPStatus.FORBIDDEN, "Invalid host")
                return
            if is_asset:
                self._frontend_asset(path)
                return
            if path == "/api/ping":
                # Unauthenticated so a second launch can find this dashboard instead of starting another.
                self._json(HTTPStatus.OK, {"app": "ai-team-chat" if chat_only else "ai-team", "root": str(app.root)})
                return
            if is_preview or path in DEFAULT_PAGES:
                try:
                    template = preview_index.read_text(encoding="utf-8")
                except FileNotFoundError:
                    template = ""
                if template and (is_preview or DEFAULT_MODE_MARKER in template):
                    self._frontend_page(template, "code" if path == "/" and not chat_only else "chat")
                    return
                if is_preview:
                    self._error(HTTPStatus.NOT_FOUND, "Build the dashboard with npm --prefix frontend run build.")
                    return
            if path in DEFAULT_PAGES | CLASSIC_PAGES:
                page_name = "chat.html" if chat_only or path in {"/chat", "/chat/", "/classic/chat"} else "index.html"
                page = web.joinpath(page_name).read_text(encoding="utf-8")
                classic = path in CLASSIC_PAGES
                encoded = (page.replace("__AI_TEAM_TOKEN__", app.token)
                           .replace("__CODE_LINK_HIDDEN__", "hidden" if chat_only else "")
                           .replace("__CODE_MODE_URL__", "/classic" if classic else "/")
                           .replace("__CHAT_MODE_URL__", "/classic/chat" if classic else "/chat")
                           .replace("__MODERN_CHAT_LINK_HIDDEN__", "" if preview_index.is_file() else "hidden")
                           .encode("utf-8"))
                self._web_content(encoded, "text/html; charset=utf-8",
                                  policy=f"{PAGE_POLICY}; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
                return
            if not self._authorized():
                self._error(HTTPStatus.FORBIDDEN, "Unauthorized")
                return
            try:
                live = SESSION_LIVE_PATH.fullmatch(path)
                if live and live[2] == "events":
                    if not self._local_origin():
                        self._error(HTTPStatus.FORBIDDEN, "Invalid origin")
                        return
                    after = parse_qs(url.query, keep_blank_values=True).get("after", [])
                    if after and (len(after) != 1 or not re.fullmatch(r"[0-9]{1,16}", after[0])):
                        self._error(HTTPStatus.BAD_REQUEST, "Invalid event cursor")
                        return
                    result = app.chat.events(live[1], int(after[0]) if after else None)
                    self._json(HTTPStatus.OK, result)
                    return
                if path == MODELS_PATH:
                    self._json(HTTPStatus.OK, app.chat.models())
                    return
                if path == SESSIONS_PATH:
                    self._json(HTTPStatus.OK, app.chat.list())
                    return
                if path == TEAMS_PATH:
                    self._json(HTTPStatus.OK, app.chat.teams())
                    return
                output = SESSION_OUTPUT_PATH.fullmatch(path)
                linked = LINKED_OUTPUT_PATH.fullmatch(path)
                if (output or linked) and not self._local_origin():
                    self._error(HTTPStatus.FORBIDDEN, "Invalid origin")
                    return
                if output and output[2] == "export":
                    query = parse_qs(url.query, keep_blank_values=True)
                    format = query.get("format", ["markdown"])
                    diagnostics = query.get("diagnostics", ["0"])
                    if format not in (["markdown"], ["json"]) or diagnostics not in (["0"], ["1"]):
                        self._error(HTTPStatus.BAD_REQUEST, "Choose Markdown or JSON and whether to include diagnostics")
                        return
                    extension, media = ("json", "application/json") if format[0] == "json" else ("md", "text/markdown")
                    result = export(app.chat.get(output[1]), format[0], diagnostics[0] == "1")
                    self._web_content(result.encode("utf-8"), media + "; charset=utf-8",
                                      filename=f"conversation-{output[1]}.{extension}")
                    return
                if linked:
                    with closing(ChatStore(app.chat.path)) as store, store.db:
                        store.db.execute("BEGIN")
                        result = store.linked_outputs(*linked.groups())
                    self._json(HTTPStatus.OK, result)
                    return
                if output and output[2] == "handoff" and not chat_only:
                    self._json(HTTPStatus.OK, app.handoff_draft(output[1]))
                    return
                session = SESSION_PATH.fullmatch(path)
                if session and not session[2]:
                    self._json(HTTPStatus.OK, app.chat.get(session[1]))
                    return
                if chat_only:
                    self._error(HTTPStatus.NOT_FOUND, "Not found")
                    return
                if path == "/api/overview":
                    self._json(HTTPStatus.OK, app.overview())
                    return
                match = TASK_PATH.fullmatch(path)
                if match and match.group(2) is None:
                    self._json(HTTPStatus.OK, app.task(match.group(1)))
                elif match and match.group(2) == "diff":
                    self._json(HTTPStatus.OK, app.diff(match.group(1)))
                else:
                    self._error(HTTPStatus.NOT_FOUND, "Not found")
            except KeyError:
                self._error(HTTPStatus.NOT_FOUND, "Conversation not found")
            except ChatBusy as error:
                self._error(HTTPStatus.SERVICE_UNAVAILABLE, str(error))
            except ValueError as error:
                self._error(HTTPStatus.NOT_FOUND, str(error))
            except Exception:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "Unable to load data")

        def do_POST(self) -> None:
            if not self._authorized():
                self._error(HTTPStatus.FORBIDDEN, "Unauthorized")
                return
            if not self._local_origin():
                self._error(HTTPStatus.FORBIDDEN, "Invalid origin")
                return
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Expected JSON")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > MAX_BODY_BYTES:
                    self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request too large")
                    return
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("Expected a JSON object")
                path = urlsplit(self.path).path
                if path == "/api/shutdown":
                    self._json(HTTPStatus.ACCEPTED, {"action": "shutdown"})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
                if path == SESSIONS_PATH:
                    self._json(HTTPStatus.CREATED, {"session": app.chat.create(body)})
                    return
                if path == TEAMS_PATH:
                    self._json(HTTPStatus.CREATED, {"team": app.chat.save_team(body)})
                    return
                output = SESSION_OUTPUT_PATH.fullmatch(path)
                if output and output[2] == "branch":
                    self._json(HTTPStatus.CREATED, {"session": app.chat.branch(output[1], body)})
                    return
                if output and output[2] == "handoff" and not chat_only:
                    self._json(HTTPStatus.CREATED, {"task_id": app.handoff(output[1], body)})
                    return
                session = SESSION_PATH.fullmatch(path)
                if session and session[2]:
                    self._json(HTTPStatus.ACCEPTED, app.chat.send(session[1], body))
                    return
                live = SESSION_LIVE_PATH.fullmatch(path)
                if live and live[2] == "cancel":
                    self._json(HTTPStatus.ACCEPTED, app.chat.cancel(live[1], body))
                    return
                run = RUN_ACTION_PATH.fullmatch(path)
                if run:
                    self._json(HTTPStatus.ACCEPTED, app.chat.control(*run.groups(), body))
                    return
                if chat_only:
                    self._error(HTTPStatus.NOT_FOUND, "Not found")
                    return
                if path == "/api/tasks":
                    task_id = app.create(body.get("prompt"), body.get("roles"), body.get("recipient", "all"))
                    self._json(HTTPStatus.ACCEPTED, {"task_id": task_id})
                    return
                match = TASK_PATH.fullmatch(path)
                if match and match.group(2) == "message":
                    app.message(match.group(1), body.get("message"), body.get("recipient", "all"))
                    self._json(HTTPStatus.ACCEPTED, {"task_id": match.group(1), "action": "message"})
                    return
                if match and match.group(2) == "guide":
                    app.guide(match.group(1), body.get("message"), body.get("recipient", "all"))
                    self._json(HTTPStatus.ACCEPTED, {"task_id": match.group(1), "action": "guide"})
                    return
                if match and match.group(2) in {"resume", "retry", "stop"}:
                    app.action(match.group(1), match.group(2))
                    self._json(HTTPStatus.ACCEPTED, {"task_id": match.group(1), "action": match.group(2)})
                    return
                self._error(HTTPStatus.NOT_FOUND, "Not found")
            except KeyError:
                self._error(HTTPStatus.NOT_FOUND, "Conversation not found")
            except ChatConflict as error:
                self._error(HTTPStatus.CONFLICT, str(error))
            except ChatBusy as error:
                self._error(HTTPStatus.SERVICE_UNAVAILABLE, str(error))
            except ValueError as error:
                self._error(HTTPStatus.BAD_REQUEST, str(error))
            except Exception:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "Unable to run action")

    server = LocalServer(("127.0.0.1", port), Handler)
    server.app = app  # type: ignore[attr-defined]
    return server


def running_instance(root: Path, port: int, *, chat_only: bool = False) -> bool:
    """Return whether the dashboard for this project is already listening on port."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
    try:
        connection.request("GET", "/api/ping")
        response = connection.getresponse()
        data = json.loads(response.read())
        expected = "ai-team-chat" if chat_only else "ai-team"
        return response.status == 200 and data.get("app") == expected and Path(data["root"]) == root.resolve()
    except (OSError, ValueError, KeyError, TypeError):
        return False
    finally:
        connection.close()


def _reopen(root: Path, port: int, open_browser: bool, *, chat_only: bool = False) -> bool:
    if not running_instance(root, port, chat_only=chat_only):
        return False
    url = f"http://127.0.0.1:{port}/"
    print(f"AI Team is already running at {url}")
    if open_browser:
        webbrowser.open(url)
    return True


def run_ui(root: Path, port: int = DEFAULT_PORT, open_browser: bool = True, *, chat_only: bool = False) -> None:
    # A nonzero port starts a short search so each project keeps a stable address and a
    # second launch reopens the existing dashboard. Port 0 always picks a fresh free port.
    team_dir(root).mkdir(exist_ok=True)
    port_file = team_dir(root) / ("chat-port" if chat_only else "ui-port")
    try:
        recorded = int(port_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        recorded = None
    if port and recorded and _reopen(root, recorded, open_browser, chat_only=chat_only):
        return
    server = None
    for candidate in range(port, port + PORT_ATTEMPTS) if port else [0]:
        try:
            server = make_server(root, candidate, chat_only=chat_only)
            break
        except OSError:
            # Binding first keeps a normal launch instant; probing a closed port on Windows takes a second.
            if candidate and _reopen(root, candidate, open_browser, chat_only=chat_only):
                return
    if server is None:
        raise RuntimeError(f"Ports {port}-{port + PORT_ATTEMPTS - 1} are in use; pass --port 0 to pick a free port")
    port_file.write_text(str(server.server_port), encoding="utf-8")
    if not chat_only:
        threading.Thread(target=server.app.refresh_quotas, daemon=True, name="ai-team-usage").start()
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"AI Team is running at {url}")
    print("Press Ctrl+C or use Quit in the dashboard to stop. Agent turns still running are interrupted on exit.")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        port_file.unlink(missing_ok=True)
