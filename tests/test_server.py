from __future__ import annotations

import http.client
import json
import yaml
import subprocess
import tempfile
import threading
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from ai_team.chat_store import ChatStore
from ai_team.config import DEFAULT_CONFIG, initialize
from ai_team.server import make_server, run_ui, running_instance
from ai_team.store import Store
from test_chat import KEYS, OPENAI, api_keys


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for command in (["init", "-q"], ["config", "user.email", "test@example.invalid"],
                        ["config", "user.name", "Test"]):
            subprocess.run(["git", *command], cwd=self.root, check=True, capture_output=True)
        (self.root / "README.md").write_text("test\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.root, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "initial"], cwd=self.root, check=True, capture_output=True)
        initialize(self.root)
        store = Store(self.root / ".ai-team" / "state.sqlite3")
        self.task = store.create_task("Inspect parser", DEFAULT_CONFIG)
        store.close()
        self.server = make_server(self.root, 0, "test-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, method: str, path: str, body: dict | None = None, token: str | None = "test-token",
                origin: str | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        headers = {}
        if token is not None:
            headers["X-AI-Team-Token"] = token
        if origin is not None:
            headers["Origin"] = origin
        payload = None if body is None else json.dumps(body)
        if payload is not None:
            headers["Content-Type"] = "application/json"
        connection.request(method, path, payload, headers)
        response = connection.getresponse()
        data = response.read().decode("utf-8")
        connection.close()
        return response.status, data

    def test_dashboard_and_task_views(self):
        status, page = self.request("GET", "/", token=None)
        self.assertEqual(status, 200)
        self.assertIn("AI Team", page)
        self.assertIn("test-token", page)
        status, body = self.request("GET", "/api/overview")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["tasks"][0]["id"], self.task["id"])
        status, body = self.request("GET", f"/api/tasks/{self.task['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["messages"][0]["sender"], "human")
        self.assertFalse(json.loads(body)["task"]["can_retry"])
        status, body = self.request("GET", f"/api/tasks/{self.task['id']}/diff")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["changes"], {})
        with self.server.app.coordinator() as coordinator:
            worktree = coordinator.git.ensure(self.task["id"], "codex")
            (worktree / "README.md").write_text("changed in the worktree\n", encoding="utf-8")
        status, body = self.request("GET", f"/api/tasks/{self.task['id']}/diff")
        self.assertEqual(status, 200)
        change = json.loads(body)["changes"]["codex"]
        self.assertEqual(change["files"], ["README.md"])
        self.assertIn("+changed in the worktree", change["diff"])

    def test_overview_reports_recorded_tokens_and_quota(self):
        store = Store(self.root / ".ai-team" / "state.sqlite3")
        store.record_invocation({"id": "usage-test", "task_id": self.task["id"], "agent": "codex",
                                 "started_at": "2026-01-01T00:00:00Z", "duration_seconds": 1.0,
                                 "exit_code": 0, "prompt": "task", "raw_output": "", "stderr": "",
                                 "response": json.dumps({"usage": {"input_tokens": 20, "output_tokens": 4}}),
                                 "diff_before": "", "diff_after": ""})
        store.save_quota("codex", {"checked_at": "2026-01-01T00:00:00Z", "source": "test",
                                   "windows": [{"label": "5h", "remaining_percent": 88}]})
        store.close()
        status, body = self.request("GET", "/api/overview")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["usage"]["codex"], 24)
        self.assertEqual(json.loads(body)["quotas"]["codex"]["windows"][0]["remaining_percent"], 88)

    def test_task_view_reports_budget_used_remaining_and_pause_reason(self):
        config_path = self.root / ".ai-team" / "config.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config["limits"]["budget"] = {"tokens": 40, "cost_usd": 1.0}
        config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
        store = Store(self.root / ".ai-team" / "state.sqlite3")
        task = store.create_task("Budgeted task", config)
        task["pause_reason"] = "Token budget reached"
        store.save_task(task)
        store.record_invocation({"id": "budget-test", "task_id": task["id"], "agent": "codex",
                                 "started_at": "2026-01-01T00:00:00Z", "duration_seconds": 1.0,
                                 "exit_code": 0, "prompt": "task", "raw_output": "", "stderr": "",
                                 "response": json.dumps({"usage": {"total_tokens": 30, "cost_usd": 0.25}}),
                                 "diff_before": "", "diff_after": ""})
        store.close()
        status, body = self.request("GET", f"/api/tasks/{task['id']}")
        self.assertEqual(status, 200)
        shown = json.loads(body)["task"]
        self.assertEqual(shown["budget_summary"]["used"], {"tokens": 30, "cost_usd": 0.25})
        self.assertEqual(shown["budget_summary"]["remaining"], {"tokens": 10, "cost_usd": 0.75})
        self.assertEqual(shown["pause_reason"], "Token budget reached")

    def test_token_and_origin_guard_mutations(self):
        status, _ = self.request("GET", "/api/overview", token=None)
        self.assertEqual(status, 403)
        status, _ = self.request("POST", f"/api/tasks/{self.task['id']}/stop", {}, origin="http://evil.invalid")
        self.assertEqual(status, 403)
        status, _ = self.request("POST", f"/api/tasks/{self.task['id']}/stop", {})
        self.assertEqual(status, 202)
        store = Store(self.root / ".ai-team" / "state.sqlite3")
        self.assertEqual(store.get_task(self.task["id"])["status"], "stopped")
        store.close()

    def test_create_task_queues_background_run(self):
        launched = []
        with patch("ai_team.server.available_agents", return_value={"codex": True, "claude": True, "gemini": True}), \
             patch.object(self.server.app, "_launch", side_effect=lambda task_id, action: launched.append((task_id, action))):
            status, body = self.request("POST", "/api/tasks", {"prompt": "Review the docs"})
        self.assertEqual(status, 202)
        task_id = json.loads(body)["task_id"]
        self.assertEqual(launched, [(task_id, "resume")])
        store = Store(self.root / ".ai-team" / "state.sqlite3")
        self.assertEqual(store.get_task(task_id)["original_prompt"], "Review the docs")
        store.close()

    def test_handoff_requires_confirmation_and_links_approved_brief_and_saved_artifacts(self):
        path = self.server.app.chat.path
        with closing(ChatStore(path)) as chat, chat.db:
            session = chat.create(**OPENAI, settings={"system_prompt": "", "max_turns": 4,
                                                    "max_output_tokens": 4096, "timeout_seconds": 60})
            turn = chat.add_turn(session, uuid.uuid4().hex, **OPENAI, prompt="Improve parser handling", owner="test",
                                 context_turns=0, truncated=False)
        with closing(ChatStore(path)) as chat:
            chat.finish_turn(turn, response="Keep the public interface")
        prefix = f"/api/v1/sessions/{session['id']}"
        with (
            patch("ai_team.server.available_agents", return_value={"codex": True, "claude": True, "gemini": True}),
            patch.object(self.server.app, "_launch") as launch,
        ):
            status, data = self.request("GET", prefix + "/handoff")
            self.assertEqual(status, 200)
            preview = json.loads(data)
            self.assertEqual(preview["brief"]["objective"], "Improve parser handling")
            self.assertIn("Keep the public interface", preview["brief"]["context"])
            payload = {"request_id": uuid.uuid4().hex, "repository": preview["repository"],
                       "brief": {**preview["brief"], "acceptance_criteria": "Existing examples still parse"},
                       "roles": preview["roles"], "recipient": "codex"}
            self.assertEqual(self.request("POST", prefix + "/handoff", payload)[0], 409)
            self.assertEqual(self.request("POST", prefix + "/handoff",
                             {**payload, "confirmed": True, "repository": "other-repository"})[0], 409)
            launch.assert_not_called()
            self.assertEqual(json.loads(self.request("GET", prefix)[1])["handoffs"], [])
            payload["confirmed"] = True
            self.assertEqual(self.request("POST", prefix + "/handoff", payload, token=None)[0], 403)
            status, created = self.request("POST", prefix + "/handoff", payload)
            self.assertEqual(status, 201)
            task_id = json.loads(created)["task_id"]
            self.assertEqual(self.request("POST", prefix + "/handoff", payload), (201, created))
            launch.assert_called_once_with(task_id, "resume")

        detail = json.loads(self.request("GET", f"/api/tasks/{task_id}")[1])
        self.assertEqual(detail["source"]["id"], session["id"])
        self.assertIn("Existing examples still parse", detail["task"]["original_prompt"])
        self.assertEqual(json.loads(self.request("GET", prefix)[1])["handoffs"][0]["task_id"], task_id)
        with closing(Store(path)) as store:
            task = store.get_task(task_id)
            task["artifacts"].append({"agent": "codex", "tests": [{"command": ["pytest"], "exit_code": 0,
                                                                  "output": "private raw test output"}]})
            store.save_task(task)
            store.add_message(task_id, "claude", "all", "Reviewed the boundary conditions")
            store.record_invocation({"id": "handoff-test", "task_id": task_id, "agent": "codex",
                                     "started_at": "2026-01-01T00:00:00Z", "duration_seconds": 1.0,
                                     "exit_code": 0, "prompt": "", "raw_output": "private stdout", "stderr": "",
                                     "response": json.dumps({"files_changed": ["parser.py"]}),
                                     "diff_before": "", "diff_after": f"+{KEYS['OPENAI_API_KEY']}"})
        self.assertEqual(self.request("GET", f"{prefix}/artifacts/{self.task['id']}")[0], 404)
        with api_keys(KEYS):
            status, data = self.request("GET", f"{prefix}/artifacts/{task_id}")
        self.assertEqual(status, 200)
        self.assertNotIn(KEYS["OPENAI_API_KEY"], data)
        self.assertNotIn("private", data)
        artifact = json.loads(data)
        self.assertEqual(artifact["changes"]["codex"]["files"], ["parser.py"])
        self.assertEqual(artifact["changes"]["codex"]["diff"], "+[API key redacted]")
        self.assertEqual(artifact["updates"][0]["content"], "Reviewed the boundary conditions")
        self.assertEqual(artifact["tests"][0]["exit_code"], 0)

    def test_download_formats_exclude_diagnostics_unless_requested(self):
        with closing(ChatStore(self.server.app.chat.path)) as chat, chat.db:
            session = chat.create(**OPENAI, settings={"system_prompt": "", "max_turns": 4,
                                                    "max_output_tokens": 4096, "timeout_seconds": 60})
            turn = chat.add_turn(session, uuid.uuid4().hex, **OPENAI, prompt="Explain how the parser works", owner="test",
                                 context_turns=0, truncated=False)
        with closing(ChatStore(self.server.app.chat.path)) as chat:
            chat.finish_turn(turn, error=f"Provider failed with {KEYS['OPENAI_API_KEY']}", total_tokens=7)
        endpoint = f"/api/v1/sessions/{session['id']}/export"
        self.assertEqual(self.request("GET", endpoint, token=None)[0], 403)
        self.assertEqual(self.request("GET", endpoint, origin="http://other.invalid")[0], 403)
        self.assertEqual(self.request("GET", endpoint + "?format=html")[0], 400)
        status, markdown = self.request("GET", endpoint)
        self.assertEqual(status, 200)
        self.assertIn("## Turn 1 · You", markdown)
        self.assertNotIn("Provider failed", markdown)
        status, portable = self.request("GET", endpoint + "?format=json")
        self.assertEqual(status, 200)
        self.assertNotIn("total_tokens", json.loads(portable)["turns"][0])
        with api_keys(KEYS):
            status, data = self.request("GET", endpoint + "?format=json&diagnostics=1")
        self.assertEqual(status, 200)
        self.assertNotIn(KEYS["OPENAI_API_KEY"], data)
        self.assertEqual(json.loads(data)["turns"][0]["total_tokens"], 7)
        self.assertIn("Provider failed with [API key redacted]", data)

    def test_create_task_saves_selected_roles_and_activity_is_visible(self):
        roles = {"codex": "q&a", "claude": "implementation", "gemini": "qa"}
        with patch("ai_team.server.available_agents", return_value={"codex": True, "claude": True, "gemini": True}), \
             patch.object(self.server.app, "_launch"):
            status, body = self.request("POST", "/api/tasks", {"prompt": "Build it", "roles": roles})
        self.assertEqual(status, 202)
        task_id = json.loads(body)["task_id"]
        self.server.app._activity(task_id, "claude", "thinking and responding")
        shown = json.loads(self.request("GET", f"/api/tasks/{task_id}")[1])["task"]
        self.assertEqual(shown["roles"], roles)
        self.assertEqual(shown["queue"][0]["agent"], "claude")
        self.assertEqual(shown["activity"], {"agent": "claude", "stage": "thinking and responding"})
        overview = json.loads(self.request("GET", "/api/overview")[1])
        self.assertEqual(overview["activity"][task_id]["agent"], "claude")

    def test_new_task_can_target_one_agent(self):
        launched = []
        with patch("ai_team.server.available_agents", return_value={"codex": True, "claude": True, "gemini": True}), \
             patch.object(self.server.app, "_launch", side_effect=lambda task_id, action: launched.append((task_id, action))):
            status, body = self.request("POST", "/api/tasks", {"prompt": "Check the parser", "recipient": "claude"})
        self.assertEqual(status, 202)
        task_id = json.loads(body)["task_id"]
        store = Store(self.root / ".ai-team" / "state.sqlite3")
        self.assertEqual([item["agent"] for item in store.get_task(task_id)["queue"]], ["claude"])
        self.assertEqual(store.messages(task_id)[0]["recipient"], "claude")
        store.close()

    def test_message_to_existing_chat_queues_selected_agent(self):
        store = Store(self.root / ".ai-team" / "state.sqlite3")
        task = store.get_task(self.task["id"])
        task["status"] = "review_ready"
        task["queue"] = []
        store.save_task(task)
        store.close()
        launched = []
        with patch.object(self.server.app, "_launch", side_effect=lambda task_id, action: launched.append((task_id, action))):
            status, _ = self.request("POST", f"/api/tasks/{self.task['id']}/message",
                                     {"message": "Check this edge case", "recipient": "gemini"})
        self.assertEqual(status, 202)
        self.assertEqual(launched, [(self.task["id"], "resume")])
        store = Store(self.root / ".ai-team" / "state.sqlite3")
        saved = store.get_task(self.task["id"])
        self.assertEqual(saved["status"], "running")
        self.assertEqual([item["agent"] for item in saved["queue"]], ["gemini"])
        self.assertEqual(store.messages(self.task["id"])[-1]["recipient"], "gemini")
        store.close()

    def test_guidance_for_paused_task_queues_resume(self):
        store = Store(self.root / ".ai-team" / "state.sqlite3")
        task = store.get_task(self.task["id"])
        task["status"] = "awaiting_human"
        store.save_task(task)
        store.close()
        launched = []
        with patch.object(self.server.app, "_launch", side_effect=lambda task_id, action: launched.append((task_id, action))):
            status, _ = self.request("POST", f"/api/tasks/{self.task['id']}/guide",
                                     {"message": "Check the parser", "recipient": "codex"})
        self.assertEqual(status, 202)
        self.assertEqual(launched, [(self.task["id"], "resume")])
        store = Store(self.root / ".ai-team" / "state.sqlite3")
        self.assertEqual(store.get_task(self.task["id"])["status"], "running")
        self.assertEqual(store.messages(self.task["id"])[-1]["content"], "Check the parser")
        store.close()

    def test_setup_failure_can_be_retried(self):
        store = Store(self.root / ".ai-team" / "state.sqlite3")
        task = store.get_task(self.task["id"])
        task["status"] = "failed"
        store.save_task(task)
        store.close()
        status, body = self.request("GET", f"/api/tasks/{self.task['id']}")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["task"]["can_retry"])
        launched = []
        with patch.object(self.server.app, "_launch", side_effect=lambda task_id, action: launched.append((task_id, action))):
            status, _ = self.request("POST", f"/api/tasks/{self.task['id']}/retry", {})
        self.assertEqual(status, 202)
        self.assertEqual(launched, [(self.task["id"], "retry")])

    def test_ping_identifies_project_without_token(self):
        status, body = self.request("GET", "/api/ping", token=None)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"app": "ai-team", "root": str(self.root.resolve())})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.request("GET", "/api/ping", headers={"Host": "evil.invalid"})
        self.assertEqual(connection.getresponse().status, 403)
        connection.close()
        status, body = self.request("GET", "/api/overview")
        self.assertEqual(json.loads(body)["project"]["name"], self.root.resolve().name)

    def test_second_launch_reuses_running_dashboard(self):
        port = self.server.server_port
        self.assertTrue(running_instance(self.root, port))
        with tempfile.TemporaryDirectory() as other:
            self.assertFalse(running_instance(Path(other), port))
        with self.assertRaises(OSError):
            make_server(self.root, port).server_close()
        with patch("ai_team.server.webbrowser.open") as opened:
            run_ui(self.root, port)
        opened.assert_called_once_with(f"http://127.0.0.1:{port}/")

    def test_launch_reopens_recorded_port_before_searching(self):
        # The dashboard may sit above the default port if that port was busy when it started.
        (self.root / ".ai-team" / "ui-port").write_text(str(self.server.server_port), encoding="utf-8")
        with patch("ai_team.server.webbrowser.open") as opened, \
             patch("ai_team.server.make_server", side_effect=AssertionError("started a second server")):
            run_ui(self.root, 8765)
        opened.assert_called_once_with(f"http://127.0.0.1:{self.server.server_port}/")

    def test_shutdown_requires_token_and_stops_server(self):
        status, _ = self.request("POST", "/api/shutdown", {}, token=None)
        self.assertEqual(status, 403)
        status, _ = self.request("POST", "/api/shutdown", {})
        self.assertEqual(status, 202)
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive())

    def test_single_recipient_only_needs_that_cli(self):
        launched = []
        with patch("ai_team.server.available_agents", return_value={"codex": True, "claude": True, "gemini": False}), \
             patch.object(self.server.app, "_launch", side_effect=lambda task_id, action: launched.append((task_id, action))):
            status, _ = self.request("POST", "/api/tasks", {"prompt": "Implement it", "recipient": "codex"})
            self.assertEqual(status, 202)
            status, body = self.request("POST", "/api/tasks", {"prompt": "Review it"})
        self.assertEqual(status, 400)
        self.assertIn("gemini", json.loads(body)["error"])
        self.assertEqual(len(launched), 1)


if __name__ == "__main__":
    unittest.main()
