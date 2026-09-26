from __future__ import annotations

import copy
import subprocess
import tempfile
import unittest
from pathlib import Path

from ai_team.adapters import AgentResponse, filtered_environment, parse_response
from ai_team.config import DEFAULT_CONFIG, initialize
from ai_team.coordinator import Coordinator


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


class FakeAdapter:
    calls: list[tuple[str, str]] = []
    scripted: dict[str, list[AgentResponse]] = {}

    def __init__(self, name, settings, allowed_environment, timeout):
        self.name = name
        self.settings = settings

    def run(self, task, context, history, worktree):
        self.calls.append((self.name, context))
        response = self.scripted[self.name].pop(0)
        return response, {"prompt": context, "exit_code": 0, "duration_seconds": 0.01,
                          "stderr": "", "raw_output": response.message}


class FailOnceAdapter(FakeAdapter):
    failed = False

    def run(self, task, context, history, worktree):
        if self.name == "codex" and not self.failed:
            FailOnceAdapter.failed = True
            self.calls.append((self.name, context))
            return AgentResponse("codex", "Connection failed", status="blocked"), {
                "prompt": context, "exit_code": 1, "duration_seconds": 0.01,
                "stderr": "Connection failed", "raw_output": ""}
        return super().run(task, context, history, worktree)


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        git(self.root, "init", "-q")
        git(self.root, "config", "user.email", "test@example.invalid")
        git(self.root, "config", "user.name", "Test")
        (self.root / "README.md").write_text("test\n", encoding="utf-8")
        git(self.root, "add", "README.md")
        git(self.root, "commit", "-qm", "initial")
        initialize(self.root)
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        FakeAdapter.calls = []

    def tearDown(self):
        for coordinator in getattr(self, "coordinators", []):
            coordinator.store.close()
        self.temp.cleanup()

    def coordinator(self):
        coordinator = Coordinator(self.root, self.config, FakeAdapter)
        self.coordinators = getattr(self, "coordinators", []) + [coordinator]
        return coordinator

    def test_fixed_conversation_persists_and_passes_prior_findings(self):
        FakeAdapter.scripted = {
            "codex": [AgentResponse("codex", "found issue", status="complete")],
            "claude": [AgentResponse("claude", "reviewed issue", status="complete")],
            "gemini": [AgentResponse("gemini", "verified issue", status="complete")],
        }
        coordinator = self.coordinator()
        task = coordinator.start("Inspect parser")
        self.assertEqual(task["status"], "complete")
        self.assertEqual([agent for agent, _ in FakeAdapter.calls], ["codex", "claude", "gemini"])
        self.assertIn("found issue", FakeAdapter.calls[1][1])
        self.assertIn("reviewed issue", FakeAdapter.calls[2][1])
        self.assertEqual(len(coordinator.store.messages(task["id"])), 4)
        self.assertTrue((self.root / ".ai-team" / "logs" / f"{task['id']}.jsonl").exists())
        self.assertEqual(self.coordinator().store.get_task(task["id"])["turn_count"], 3)

    def test_delegation_adds_turn_and_deduplicates(self):
        FakeAdapter.scripted = {
            "codex": [AgentResponse("codex", "Need check", "gemini", "Check boundary", status="working"),
                      AgentResponse("codex", "Need check", "gemini", "Check boundary", status="complete")],
            "claude": [AgentResponse("claude", "Architecture okay", status="complete")],
            "gemini": [AgentResponse("gemini", "Initial QA", "codex", "Check boundary", status="working"),
                       AgentResponse("gemini", "Boundary verified", status="complete")],
        }
        coordinator = self.coordinator()
        task = coordinator.start("Inspect parser")
        self.assertEqual([agent for agent, _ in FakeAdapter.calls], ["codex", "claude", "gemini", "gemini", "codex"])
        self.assertEqual(task["turn_count"], 5)
        self.assertEqual(task["status"], "review_ready")

    def test_structured_and_mention_parsing(self):
        response = parse_response("claude", '{"message":"issue","delegate_to":"codex","delegated_task":"Fix it","status":"working"}', "claude-json")
        self.assertEqual((response.requested_agent, response.requested_task), ("codex", "Fix it"))
        response = parse_response("gemini", '{"response":"@claude Please review the diff"}', "gemini-json")
        self.assertEqual((response.requested_agent, response.requested_task), ("claude", "Please review the diff"))

    def test_antigravity_envelope_and_terminal_failure(self):
        response = parse_response("gemini", '{"status":"SUCCESS","response":"{\\"message\\":\\"Verified\\",\\"delegate_to\\":\\"codex\\",\\"delegated_task\\":\\"Check case\\"}","usage":{"total_tokens":14}}', "antigravity-json")
        self.assertEqual(response.message, "Verified")
        self.assertEqual(response.requested_agent, "codex")
        self.assertEqual(response.usage["total_tokens"], 14)
        response = parse_response("gemini", '{"status":"ERROR","error":"authentication required","response":""}', "antigravity-json")
        self.assertEqual(response.status, "blocked")
        self.assertIn("authentication required", response.message)
        response = parse_response("gemini", '{"status":"SUCCESS","response":"ignored","structured_output":{"message":"Checked","status":"complete"}}', "antigravity-json")
        self.assertEqual((response.message, response.status), ("Checked", "complete"))

    def test_failed_cli_turn_can_retry_without_losing_queue(self):
        FailOnceAdapter.failed = False
        FakeAdapter.scripted = {
            "codex": [AgentResponse("codex", "Finding", status="complete")],
            "claude": [AgentResponse("claude", "Review", status="complete")],
            "gemini": [AgentResponse("gemini", "QA", status="complete")],
        }
        coordinator = Coordinator(self.root, self.config, FailOnceAdapter)
        self.coordinators = getattr(self, "coordinators", []) + [coordinator]
        task = coordinator.start("Inspect parser")
        self.assertEqual(task["status"], "failed")
        self.assertEqual([item["agent"] for item in task["queue"]], ["codex", "claude", "gemini"])
        task = coordinator.retry_failed(task["id"])
        self.assertEqual(task["status"], "complete")
        self.assertEqual([agent for agent, _ in FakeAdapter.calls], ["codex", "codex", "claude", "gemini"])

    def test_retry_recovers_old_failed_task(self):
        FailOnceAdapter.failed = False
        FakeAdapter.scripted = {
            "codex": [AgentResponse("codex", "Finding", status="complete")],
            "claude": [AgentResponse("claude", "Review", status="complete")],
            "gemini": [AgentResponse("gemini", "QA", status="complete")],
        }
        coordinator = Coordinator(self.root, self.config, FailOnceAdapter)
        self.coordinators = getattr(self, "coordinators", []) + [coordinator]
        task = coordinator.start("Inspect parser")
        task["status"] = "awaiting_human"
        task["queue"].pop(0)
        coordinator.store.save_task(task)
        task = coordinator.retry_failed(task["id"])
        self.assertEqual(task["status"], "complete")
        self.assertEqual([agent for agent, _ in FakeAdapter.calls], ["codex", "codex", "claude", "gemini"])

    def test_proxy_variables_are_in_allowlist(self):
        self.assertIn("HTTPS_PROXY", self.config["execution"]["allowed_environment"])
        self.assertIn("NO_PROXY", self.config["execution"]["allowed_environment"])

    def test_environment_allowlist_matches_windows_case(self):
        source = {"SYSTEMROOT": "C:\\Windows", "HTTPS_PROXY": "http://proxy.invalid", "SECRET": "hidden"}
        result = filtered_environment(["SystemRoot", "https_proxy"], source)
        self.assertEqual(result, {"SYSTEMROOT": "C:\\Windows", "HTTPS_PROXY": "http://proxy.invalid"})


if __name__ == "__main__":
    unittest.main()
