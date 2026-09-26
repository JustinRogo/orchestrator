from __future__ import annotations

import copy
import subprocess
import tempfile
import unittest
from pathlib import Path

from ai_team.adapters import AgentResponse, parse_response
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


if __name__ == "__main__":
    unittest.main()
