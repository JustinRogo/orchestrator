from __future__ import annotations

import http.client
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_team.config import DEFAULT_CONFIG, initialize
from ai_team.server import make_server
from ai_team.store import Store


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

    def test_overview_reports_last_turn_usage_without_guessing_remaining(self):
        store = Store(self.root / ".ai-team" / "state.sqlite3")
        store.record_invocation({"id": "usage-test", "task_id": self.task["id"], "agent": "codex",
                                 "started_at": "2026-01-01T00:00:00Z", "duration_seconds": 1.0,
                                 "exit_code": 0, "prompt": "task", "raw_output": "", "stderr": "",
                                 "response": json.dumps({"usage": {"input_tokens": 20, "output_tokens": 4}}),
                                 "diff_before": "", "diff_after": ""})
        store.close()
        status, body = self.request("GET", "/api/overview")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["usage"]["codex"], 24)
        self.assertNotIn("remaining", json.loads(body))

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


if __name__ == "__main__":
    unittest.main()
