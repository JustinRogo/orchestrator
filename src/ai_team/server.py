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
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

from .config import available_agents, load, team_dir
from .coordinator import Coordinator
from .quota import collect as collect_quota


DEFAULT_PORT = 8765
PORT_ATTEMPTS = 10
TASK_PATH = re.compile(r"^/api/tasks/([a-f0-9]{12})(?:/(diff|resume|retry|stop|guide|message))?$")
MAX_BODY_BYTES = 32_768


class WebApp:
    def __init__(self, root: Path, token: str | None = None):
        self.root = root.resolve()
        self.token = token or secrets.token_urlsafe(32)
        self.active: set[str] = set()
        self.lock = threading.Lock()

    @contextmanager
    def coordinator(self) -> Iterator[Coordinator]:
        coordinator = Coordinator(self.root, load(self.root))
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
        return {
            "project": {"name": self.root.name, "path": str(self.root)},
            "agents": available_agents(config),
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
        return {"task": task, "messages": messages}

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

    def create(self, prompt: str, roles: dict[str, str] | None = None, recipient: str = "all") -> str:
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 20_000:
            raise ValueError("Enter a task of at most 20,000 characters")
        if roles is not None and (not isinstance(roles, dict) or any(
            name not in {"codex", "claude", "gemini"} or not isinstance(role, str) or len(role) > 500
            for name, role in roles.items()
        )):
            raise ValueError("Invalid agent roles")
        config = load(self.root)
        agents = available_agents(config)
        missing = [name for name, found in agents.items() if not found and recipient in {"all", name}]
        if missing:
            raise ValueError(f"Required CLIs missing: {', '.join(missing)}. Set their command paths in .ai-team/config.yaml.")
        with self.coordinator() as coordinator:
            task = coordinator.create(prompt, roles, recipient)
        self._launch(task["id"], "resume")
        return task["id"]

    def action(self, task_id: str, action: str) -> None:
        if action == "stop":
            with self.coordinator() as coordinator:
                coordinator.stop(task_id)
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
        thread = threading.Thread(target=self._run, args=(task_id, action), daemon=True,
                                  name=f"ai-team-{task_id}")
        thread.start()

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


class LocalServer(ThreadingHTTPServer):
    # Windows SO_REUSEADDR lets a second process bind a port that is already listening,
    # which would silently split requests between two dashboards.
    allow_reuse_address = os.name != "nt"

    def server_bind(self) -> None:
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_server(root: Path, port: int = DEFAULT_PORT, token: str | None = None) -> ThreadingHTTPServer:
    app = WebApp(root, token)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def _authorized(self) -> bool:
            host = self.headers.get("Host", "")
            expected = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            return host in expected and self.headers.get("X-AI-Team-Token") == app.token

        def _json(self, status: int, body: dict[str, Any]) -> None:
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _error(self, status: int, message: str) -> None:
            self._json(status, {"error": message})

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path in {"/", "/api/ping"}:
                host = self.headers.get("Host", "")
                if host not in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}:
                    self._error(HTTPStatus.FORBIDDEN, "Invalid host")
                    return
            if path == "/api/ping":
                # Unauthenticated so a second launch can find this dashboard instead of starting another.
                self._json(HTTPStatus.OK, {"app": "ai-team", "root": str(app.root)})
                return
            if path == "/":
                page = files("ai_team").joinpath("web/index.html").read_text(encoding="utf-8")
                encoded = page.replace("__AI_TEAM_TOKEN__", app.token).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
                return
            if not self._authorized():
                self._error(HTTPStatus.FORBIDDEN, "Unauthorized")
                return
            try:
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
            except ValueError as error:
                self._error(HTTPStatus.NOT_FOUND, str(error))
            except Exception:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "Unable to load data")

        def do_POST(self) -> None:
            if not self._authorized():
                self._error(HTTPStatus.FORBIDDEN, "Unauthorized")
                return
            origin = self.headers.get("Origin")
            if origin and origin not in {f"http://127.0.0.1:{self.server.server_port}",
                                         f"http://localhost:{self.server.server_port}"}:
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
            except (ValueError, json.JSONDecodeError) as error:
                self._error(HTTPStatus.BAD_REQUEST, str(error))
            except Exception:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "Unable to run action")

    server = LocalServer(("127.0.0.1", port), Handler)
    server.app = app  # type: ignore[attr-defined]
    return server


def running_instance(root: Path, port: int) -> bool:
    """Return whether the dashboard for this project is already listening on port."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
    try:
        connection.request("GET", "/api/ping")
        response = connection.getresponse()
        data = json.loads(response.read())
        return response.status == 200 and data.get("app") == "ai-team" and Path(data["root"]) == root.resolve()
    except (OSError, ValueError, KeyError, TypeError):
        return False
    finally:
        connection.close()


def _reopen(root: Path, port: int, open_browser: bool) -> bool:
    if not running_instance(root, port):
        return False
    url = f"http://127.0.0.1:{port}/"
    print(f"AI Team is already running at {url}")
    if open_browser:
        webbrowser.open(url)
    return True


def run_ui(root: Path, port: int = DEFAULT_PORT, open_browser: bool = True) -> None:
    # A nonzero port starts a short search so each project keeps a stable address and a
    # second launch reopens the existing dashboard. Port 0 always picks a fresh free port.
    port_file = team_dir(root) / "ui-port"
    try:
        recorded = int(port_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        recorded = None
    if port and recorded and _reopen(root, recorded, open_browser):
        return
    server = None
    for candidate in range(port, port + PORT_ATTEMPTS) if port else [0]:
        try:
            server = make_server(root, candidate)
            break
        except OSError:
            # Binding first keeps a normal launch instant; probing a closed port on Windows takes a second.
            if candidate and _reopen(root, candidate, open_browser):
                return
    if server is None:
        raise RuntimeError(f"Ports {port}-{port + PORT_ATTEMPTS - 1} are in use; pass --port 0 to pick a free port")
    port_file.write_text(str(server.server_port), encoding="utf-8")
    threading.Thread(target=server.app.refresh_quotas, daemon=True, name="ai-team-usage").start()  # type: ignore[attr-defined]
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
