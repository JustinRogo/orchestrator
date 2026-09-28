from __future__ import annotations

import copy
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_team.adapters import AgentResponse, CLIAdapter, filtered_environment, parse_response
from ai_team.config import DEFAULT_CONFIG, initialize, load
from ai_team.cli import show_chat
from ai_team.coordinator import Coordinator
from ai_team.roles import turn_settings


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


class BlockingAdapter(FakeAdapter):
    entered = threading.Event()
    release = threading.Event()

    def run(self, task, context, history, worktree):
        self.entered.set()
        if not self.release.wait(10):
            raise TimeoutError("Test adapter was not released")
        return AgentResponse(self.name, "Done", status="complete"), {
            "prompt": context, "exit_code": 0, "duration_seconds": 0.01,
            "stderr": "", "raw_output": "Done"}


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
        self.assertIn("Tracked files available for read-only review", FakeAdapter.calls[2][1])
        self.assertIn("README.md", FakeAdapter.calls[2][1])
        self.assertEqual(len(coordinator.store.messages(task["id"])), 4)
        self.assertTrue((self.root / ".ai-team" / "logs" / f"{task['id']}.jsonl").exists())
        self.assertEqual(self.coordinator().store.get_task(task["id"])["turn_count"], 3)

    def test_roles_change_turn_order_permissions_and_review_source(self):
        roles = {"codex": "q&a", "claude": "implementation", "gemini": "qa"}
        FakeAdapter.scripted = {name: [AgentResponse(name, "Done", status="complete")]
                                for name in roles}
        coordinator = self.coordinator()
        events = []
        coordinator.activity = lambda task_id, agent, stage: events.append((agent, stage))
        task = coordinator.start("Implement feature", roles)
        self.assertEqual([name for name, _ in FakeAdapter.calls], ["claude", "codex", "gemini"])
        self.assertEqual(task["roles"], roles)
        self.assertTrue(turn_settings("codex", task, self.config)["read_only"])
        self.assertIn("read-only", turn_settings("codex", task, self.config)["args"])
        self.assertFalse(turn_settings("claude", task, self.config)["read_only"])
        self.assertIn("acceptEdits", turn_settings("claude", task, self.config)["args"])
        alternate = {**task, "roles": {"gemini": "implementation", "codex": "q&a", "claude": "review"}}
        self.assertIn("accept-edits", turn_settings("gemini", alternate, self.config)["args"])
        self.assertIn(("claude", "thinking and responding"), events)
        self.assertEqual(events[-1], ("", "idle"))

    def test_swapped_implementor_edits_runs_tests_and_reviewers_cannot_edit(self):
        self.config["execution"]["tests"] = [[sys.executable, "-c",
            "from pathlib import Path; assert Path('feature.txt').read_text() == 'built'"]]

        class EditingAdapter(FakeAdapter):
            def run(self, task, context, history, worktree):
                self.calls.append((self.name, context))
                if self.name == "claude":
                    (worktree / "feature.txt").write_text("built", encoding="utf-8")
                elif self.name == "codex":
                    (worktree / "intrusion.txt").write_text("changed", encoding="utf-8")
                return AgentResponse(self.name, "Done", status="complete"), {
                    "prompt": context, "exit_code": 0, "duration_seconds": 0.01,
                    "stderr": "", "raw_output": "Done"}

        coordinator = Coordinator(self.root, self.config, EditingAdapter)
        self.coordinators = getattr(self, "coordinators", []) + [coordinator]
        task = coordinator.start("Build feature", {"claude": "implementation", "codex": "qa"})
        self.assertEqual([agent for agent, _ in FakeAdapter.calls], ["claude", "codex"])
        self.assertEqual(task["status"], "awaiting_human")
        self.assertIn("Implementation agent diff for review", FakeAdapter.calls[1][1])
        self.assertIn("feature.txt", FakeAdapter.calls[1][1])
        self.assertIn("Read-only worktree was modified", coordinator.store.messages(task["id"])[-1]["content"])
        self.assertEqual(task["artifacts"][0]["tests"][0]["exit_code"], 0)

    def test_legacy_roles_and_custom_permission_flags(self):
        coordinator = self.coordinator()
        task = coordinator.create("Review legacy task", recipient="codex")
        task["roles"] = {"codex": "architecture and code review", "claude": "independent QA"}
        coordinator.store.save_task(task)
        FakeAdapter.scripted = {"codex": [AgentResponse("codex", "Done", status="complete")]}
        self.assertEqual(coordinator.resume(task["id"])["status"], "complete")
        self.config["agents"]["claude"]["args"] = ["-p"]
        with self.assertRaisesRegex(ValueError, "permission-mode"):
            turn_settings("claude", {"roles": {"claude": "implementation"}}, self.config)

    def test_activity_returns_to_idle_after_adapter_exception(self):
        class RaisingAdapter(FakeAdapter):
            def run(self, task, context, history, worktree):
                raise RuntimeError("adapter failure")

        events = []
        coordinator = Coordinator(self.root, self.config, RaisingAdapter,
                                  activity=lambda task_id, agent, stage: events.append(stage))
        self.coordinators = getattr(self, "coordinators", []) + [coordinator]
        task = coordinator.start("Failing task")
        self.assertEqual(task["status"], "failed")
        self.assertEqual(events[-1], "idle")

    def test_invalid_role_assignment_is_rejected(self):
        coordinator = self.coordinator()
        with self.assertRaisesRegex(ValueError, "Only one agent"):
            coordinator.create("Task", {"codex": "implementation", "claude": "implementation"})
        with self.assertRaisesRegex(ValueError, "Choose an enabled"):
            coordinator.create("Task", {"codex": "unknown"})
        with self.assertRaisesRegex(ValueError, "Choose an enabled"):
            coordinator.create("Task", {"unknown": "review"})
        self.config["agents"]["gemini"]["enabled"] = False
        with self.assertRaisesRegex(ValueError, "Choose an enabled"):
            coordinator.create("Task", {"gemini": "qa"})

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

    def test_disagreement_does_not_strand_queued_reviewers(self):
        FakeAdapter.scripted = {
            "codex": [AgentResponse("codex", "Implemented", status="complete")],
            "claude": [AgentResponse("claude", "I disagree", status="disagree")],
            "gemini": [AgentResponse("gemini", "Independent review", status="complete")],
        }
        task = self.coordinator().start("Review the change")
        self.assertEqual([agent for agent, _ in FakeAdapter.calls], ["codex", "claude", "gemini"])
        self.assertEqual(task["queue"], [])
        self.assertEqual(task["status"], "review_ready")

    def test_blocked_still_pauses_before_other_reviewers(self):
        FakeAdapter.scripted = {
            "codex": [AgentResponse("codex", "Need input", status="blocked")],
        }
        task = self.coordinator().start("Review the change")
        self.assertEqual(task["status"], "awaiting_human")
        self.assertEqual([item["agent"] for item in task["queue"]], ["claude", "gemini"])

    def test_budget_under_limit_runs_next_turn(self):
        self.config["limits"]["budget"] = {"tokens": 100, "cost_usd": 1.0}
        FakeAdapter.scripted = {
            "codex": [AgentResponse("codex", "Done", status="complete", usage={"total_tokens": 20, "cost_usd": 0.1})],
            "claude": [AgentResponse("claude", "Done", status="complete", usage={"total_tokens": 30, "cost_usd": 0.2})],
            "gemini": [AgentResponse("gemini", "Done", status="complete", usage={"total_tokens": 10, "cost_usd": 0.1})],
        }
        coordinator = self.coordinator()
        task = coordinator.start("Budgeted task")
        self.assertEqual(task["status"], "complete")
        remaining = coordinator.store.budget_summary(task)["remaining"]
        self.assertEqual(remaining["tokens"], 40)
        self.assertAlmostEqual(remaining["cost_usd"], 0.6)

    def test_reached_cost_budget_pauses(self):
        self.config["limits"]["budget"]["cost_usd"] = 0.25
        FakeAdapter.scripted = {"codex": [AgentResponse("codex", "Done", status="complete", usage={"cost_usd": 0.25})]}
        task = self.coordinator().start("Budgeted task")
        self.assertEqual(task["status"], "awaiting_human")
        self.assertIn("Cost budget reached", task["pause_reason"])

    def test_reached_budget_on_final_turn_still_pauses(self):
        self.config["limits"]["budget"]["tokens"] = 20
        self.config["agents"]["claude"]["enabled"] = False
        self.config["agents"]["gemini"]["enabled"] = False
        FakeAdapter.scripted = {"codex": [AgentResponse("codex", "Done", status="complete", usage={"total_tokens": 20})]}
        task = self.coordinator().start("Budgeted task")
        self.assertEqual(task["status"], "awaiting_human")
        self.assertEqual(task["queue"], [])
        self.assertIn("Token budget reached", task["pause_reason"])

    def test_reached_budget_pauses_before_next_turn_and_preserves_queue(self):
        self.config["limits"]["budget"]["tokens"] = 20
        FakeAdapter.scripted = {"codex": [AgentResponse("codex", "Done", status="complete", usage={"total_tokens": 20})]}
        coordinator = self.coordinator()
        task = coordinator.start("Budgeted task")
        self.assertEqual(task["status"], "awaiting_human")
        self.assertIn("Token budget reached", task["pause_reason"])
        self.assertEqual(task["turn_count"], 1)
        self.assertEqual([item["agent"] for item in task["queue"]], ["claude", "gemini"])
        self.assertEqual([agent for agent, _ in FakeAdapter.calls], ["codex"])
        self.assertEqual(coordinator.store.get_task(task["id"])["pause_reason"], task["pause_reason"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            show_chat(coordinator, task["id"])
        self.assertIn("Tokens budget: used 20, remaining 0 of 20", output.getvalue())
        self.assertIn("Pause reason: Token budget reached", output.getvalue())

    def test_missing_or_invalid_cost_pauses_before_next_turn(self):
        self.config["limits"]["budget"]["cost_usd"] = 1.0
        for cost_usage in ({}, {"cost_usd": "unknown"}, {"cost_usd": float("nan")}):
            with self.subTest(cost_usage=cost_usage):
                FakeAdapter.calls = []
                FakeAdapter.scripted = {"codex": [AgentResponse("codex", "Done", status="complete", usage=cost_usage)]}
                task = self.coordinator().start("Budgeted task")
                self.assertEqual(task["status"], "awaiting_human")
                self.assertIn("missing or invalid cost data", task["pause_reason"])
                self.assertEqual([agent for agent, _ in FakeAdapter.calls], ["codex"])

    def test_missing_cost_on_final_turn_still_requires_review(self):
        self.config["limits"]["budget"]["cost_usd"] = 1.0
        self.config["agents"]["claude"]["enabled"] = False
        self.config["agents"]["gemini"]["enabled"] = False
        FakeAdapter.scripted = {"codex": [AgentResponse("codex", "Done", status="complete")]}
        task = self.coordinator().start("Budgeted task")
        self.assertEqual(task["status"], "awaiting_human")
        self.assertEqual(task["queue"], [])
        self.assertIn("missing or invalid cost data", task["pause_reason"])

    def test_disabled_budget_allows_missing_usage(self):
        FakeAdapter.scripted = {agent: [AgentResponse(agent, "Done", status="complete")] for agent in ("codex", "claude", "gemini")}
        task = self.coordinator().start("Unmetered task")
        self.assertEqual(task["status"], "complete")
        self.assertIsNone(task["pause_reason"])

    def test_config_budget_validation(self):
        path = self.root / ".ai-team" / "config.yaml"
        for value in ("-1", "true", "1.5", "'many'"):
            with self.subTest(value=value):
                path.write_text(f"limits:\n  budget:\n    tokens: {value}\n", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "limits.budget.tokens"):
                    load(self.root)
        path.write_text("limits:\n  budget:\n    cost_usd: 0.5\n", encoding="utf-8")
        self.assertEqual(load(self.root)["limits"]["budget"], {"tokens": None, "cost_usd": 0.5})
        for value in ("-0.1", "true", "'.5'", ".nan"):
            with self.subTest(cost=value):
                path.write_text(f"limits:\n  budget:\n    cost_usd: {value}\n", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "limits.budget.cost_usd"):
                    load(self.root)

    def test_new_message_replaces_pending_turns_for_its_recipients(self):
        coordinator = self.coordinator()
        task = coordinator.create("Original review")
        task["status"] = "awaiting_human"
        coordinator.store.save_task(task)
        updated = coordinator.send_message(task["id"], "New direction", "claude")
        self.assertEqual([(item["agent"], item["task"]) for item in updated["queue"]],
                         [("claude", "New direction"), ("codex", "Original review"),
                          ("gemini", "Original review")])
        updated["status"] = "awaiting_human"
        coordinator.store.save_task(updated)
        updated = coordinator.send_message(task["id"], "Team update")
        self.assertEqual([(item["agent"], item["task"]) for item in updated["queue"]],
                         [(agent, "Team update") for agent in ("codex", "claude", "gemini")])

    def test_structured_and_mention_parsing(self):
        priced = parse_response("claude", '{"result":"Done","usage":{"input_tokens":2,"output_tokens":3},"total_cost_usd":0.04}', "claude-json")
        self.assertEqual(priced.usage["total_cost_usd"], 0.04)
        response = parse_response("claude", '{"message":"issue","delegate_to":"codex","delegated_task":"Fix it","status":"working"}', "claude-json")
        self.assertEqual((response.requested_agent, response.requested_task), ("codex", "Fix it"))
        response = parse_response("gemini", '{"response":"@claude Please review the diff"}', "gemini-json")
        self.assertEqual((response.requested_agent, response.requested_task), ("claude", "Please review the diff"))
        fenced = '{"result":"```json\\n{\\"message\\":\\"Review found an issue\\",\\"status\\":\\"complete\\",\\"delegate_to\\":\\"codex\\",\\"delegated_task\\":\\"Fix it\\"}\\n```"}'
        response = parse_response("claude", fenced, "claude-json")
        self.assertEqual(response.message, "Review found an issue")
        self.assertEqual((response.requested_agent, response.requested_task), ("codex", "Fix it"))

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
        response = parse_response("gemini", '{"status":"SUCCESS","response":"","denied_actions":[{"action":"command"}]}', "antigravity-json")
        self.assertEqual(response.status, "blocked")
        self.assertIn("command permission", response.message)

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

    def test_retry_recovers_worktree_setup_failure(self):
        FakeAdapter.scripted = {
            "codex": [AgentResponse("codex", "Recovered", status="complete")],
        }
        self.config["agents"]["claude"]["enabled"] = False
        self.config["agents"]["gemini"]["enabled"] = False
        coordinator = self.coordinator()
        task = coordinator.create("Inspect parser")
        task["status"] = "failed"
        coordinator.store.save_task(task)
        recovered = coordinator.retry_failed(task["id"])
        self.assertEqual(recovered["status"], "complete")
        self.assertEqual(recovered["turn_count"], 1)

    def test_new_worktrees_use_task_original_base(self):
        FakeAdapter.scripted = {
            "codex": [AgentResponse("codex", "Done", status="complete")],
            "claude": [AgentResponse("claude", "Done", status="complete")],
            "gemini": [AgentResponse("gemini", "Done", status="complete")],
        }
        coordinator = self.coordinator()
        task = coordinator.create("Inspect parser")
        original_base = task["git_state"]["base"]
        (self.root / "README.md").write_text("new main commit\n", encoding="utf-8")
        git(self.root, "commit", "-qam", "main moved")
        coordinator.resume(task["id"])
        for agent in ("codex", "claude", "gemini"):
            worktree = coordinator.git.path(task["id"], agent)
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=worktree, check=True,
                                  capture_output=True, text=True).stdout.strip()
            self.assertEqual(head, original_base)

    def test_stop_during_agent_turn_is_preserved(self):
        BlockingAdapter.entered.clear()
        BlockingAdapter.release.clear()
        owner = self.coordinator()
        task = owner.create("Inspect parser")
        completed = []

        def run():
            worker = Coordinator(self.root, self.config, BlockingAdapter)
            try:
                completed.append(worker.resume(task["id"]))
            finally:
                worker.store.close()

        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(BlockingAdapter.entered.wait(10))
            owner.stop(task["id"])
        finally:
            BlockingAdapter.release.set()
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(completed[0]["status"], "stopped")
        self.assertEqual(owner.store.get_task(task["id"])["status"], "stopped")

    def test_human_guidance_resumes_paused_task(self):
        self.config["collaboration"]["max_turns"] = 1
        FakeAdapter.scripted = {
            "codex": [AgentResponse("codex", "First pass", status="complete"),
                      AgentResponse("codex", "Follow-up complete", status="complete")],
        }
        self.config["agents"]["claude"]["enabled"] = False
        self.config["agents"]["gemini"]["enabled"] = False
        coordinator = self.coordinator()
        task = coordinator.start("Inspect parser")
        self.assertEqual(task["status"], "complete")
        task["status"] = "awaiting_human"
        coordinator.store.save_task(task)
        coordinator.guide(task["id"], "Check the edge case", "codex")
        resumed = coordinator.resume(task["id"])
        self.assertEqual(resumed["turn_count"], 2)
        self.assertEqual(resumed["status"], "complete")
        self.assertEqual(coordinator.store.messages(task["id"])[-2]["content"], "Check the edge case")

    def test_round_limit_keeps_pending_delegation(self):
        self.config["collaboration"]["max_rounds"] = 1
        self.config["agents"]["gemini"]["enabled"] = False
        FakeAdapter.scripted = {
            "codex": [AgentResponse("codex", "Need review", "claude", "Check this", status="working")],
            "claude": [AgentResponse("claude", "Initial review", status="complete")],
        }
        coordinator = self.coordinator()
        task = coordinator.start("Inspect parser")
        self.assertEqual(task["status"], "awaiting_human")
        self.assertEqual(task["queue"][0]["task"], "Check this")
        self.assertEqual(task["queue"][0]["round"], 2)

    def test_proxy_variables_are_in_allowlist(self):
        self.assertIn("HTTPS_PROXY", self.config["execution"]["allowed_environment"])
        self.assertIn("NO_PROXY", self.config["execution"]["allowed_environment"])

    def test_environment_allowlist_matches_windows_case(self):
        source = {"SYSTEMROOT": "C:\\Windows", "HTTPS_PROXY": "http://proxy.invalid", "SECRET": "hidden"}
        result = filtered_environment(["SystemRoot", "https_proxy"], source)
        self.assertEqual(result, {"SYSTEMROOT": "C:\\Windows", "HTTPS_PROXY": "http://proxy.invalid"})

    def test_cli_output_is_decoded_as_utf8(self):
        event = {"type": "item.completed", "item": {"type": "agent_message", "text": '{"message":"Ï checked it"}'}}
        output = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
        code = f"import sys; sys.stdout.buffer.write({output!r})"
        settings = {"command": sys.executable, "args": ["-c", code], "role": "reviewer",
                    "read_only": False, "prompt_mode": "stdin", "format": "codex-jsonl"}
        adapter = CLIAdapter("codex", settings, ["PATH", "SystemRoot"], 10)
        response, detail = adapter.run("task", "context", [], self.root)
        self.assertEqual(detail["exit_code"], 0)
        self.assertEqual(response.message, "Ï checked it")

    def test_antigravity_reviewer_avoids_headless_command_permissions(self):
        settings = {"command": sys.executable, "args": ["-p", "{prompt}"], "role": "reviewer",
                    "read_only": True, "prompt_mode": "argument", "format": "antigravity-json"}
        adapter = CLIAdapter("gemini", settings, ["PATH", "SystemRoot"], 10)
        result = subprocess.CompletedProcess([], 0, '{"status":"SUCCESS","response":"Reviewed"}', "")
        with patch("ai_team.adapters.subprocess.run", return_value=result):
            response, detail = adapter.run("Review files", "Tracked files: README.md", [], self.root)
        self.assertEqual(response.message, "Reviewed")
        self.assertIn("Use view_file", detail["prompt"])
        self.assertIn("Do not invoke run_command", detail["prompt"])


if __name__ == "__main__":
    unittest.main()
