from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Callable

from .adapters import ADAPTERS, AgentResponse
from .config import team_dir
from .git import CommandPolicy, GitWorkspaceManager
from .quota import collect as collect_quota
from .store import Store, now


class Coordinator:
    def __init__(self, root: Path, config: dict[str, Any], adapter_factory: Callable[..., Any] | None = None):
        self.root = root.resolve()
        self.config = config
        self.store = Store(team_dir(self.root) / "state.sqlite3")
        self.git = GitWorkspaceManager(self.root)
        self.adapter_factory = adapter_factory
        self.command_policy = CommandPolicy(config["execution"]["tests"])

    def _adapter(self, agent: str):
        settings = self.config["agents"][agent]
        factory = self.adapter_factory or ADAPTERS[agent]
        return factory(agent, settings, self.config["execution"]["allowed_environment"],
                       self.config["collaboration"]["timeout_seconds"])

    def _context(self, task: dict[str, Any], agent: str, worktree: Path) -> str:
        parts = [f"Original task: {task['original_prompt']}"]
        for name in ("project.md", "architecture.md", "decisions.md"):
            path = team_dir(self.root) / name
            if path.exists():
                parts.append(f"{name}:\n{path.read_text(encoding='utf-8')[:8000]}")
        messages = self.store.messages(task["id"])[-self.config["collaboration"]["recent_messages"]:]
        parts.append("Recent conversation:\n" + "\n".join(f"[{item['sender']} -> {item['recipient']}] {item['content']}" for item in messages))
        if agent == "gemini" and self.config["agents"][agent]["read_only"]:
            tracked = self.git.tracked_files(worktree)
            parts.append("Tracked files available for read-only review (paths relative to this worktree):\n"
                         + "\n".join(tracked[:100]))
        base = task["git_state"].get("base", "HEAD")
        diff = self.git.diff(worktree, base)
        if diff:
            parts.append("Current worktree diff (truncated):\n" + diff[:16000])
        primary = self.config["git"]["primary_implementation_agent"]
        if agent != primary:
            primary_path = self.git.path(task["id"], primary)
            if primary_path.exists():
                primary_diff = self.git.diff(primary_path, base)
                if primary_diff:
                    parts.append("Implementation agent diff for review (truncated):\n" + primary_diff[:16000])
        if task["artifacts"]:
            parts.append("Recent test results:\n" + json.dumps(task["artifacts"][-3:])[:8000])
        return "\n\n".join(parts)

    def start(self, prompt: str, roles: dict[str, str] | None = None) -> dict[str, Any]:
        task = self.create(prompt, roles)
        return self.resume(task["id"])

    def create(self, prompt: str, roles: dict[str, str] | None = None,
               recipient: str = "all") -> dict[str, Any]:
        if not prompt.strip():
            raise ValueError("Task prompt must not be empty")
        task = self.store.create_task(prompt, self.config, roles, recipient)
        task["git_state"]["base"] = self.git.head()
        self.store.save_task(task)
        return task

    def resume(self, task_id: str) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        if task["status"] in {"stopped", "complete", "awaiting_human", "failed"}:
            return task
        max_turns = self.config["collaboration"]["max_turns"] + task["git_state"].get("extra_turns", 0)
        while task["queue"] and task["turn_count"] < max_turns:
            if self.store.get_task(task_id)["status"] == "stopped":
                task["status"] = "stopped"
                break
            item = task["queue"].pop(0)
            agent = item["agent"]
            if item["round"] > task["max_rounds"]:
                task["queue"].insert(0, item)
                task["status"] = "awaiting_human"
                break
            task["current_round"] = item["round"]
            base = task["git_state"].get("base", "HEAD")
            worktree = self.git.ensure(task_id, agent, base)
            before = self.git.diff(worktree, base)
            old_status = self.git.status(worktree)
            settings = dict(self.config["agents"][agent])
            settings["role"] = task["roles"].get(agent, settings["role"])
            context = self._context(task, agent, worktree)
            adapter = self._adapter(agent)
            adapter.settings = settings
            try:
                response, detail = adapter.run(item["task"], context, self.store.messages(task_id), worktree)
            except Exception as error:
                response = AgentResponse(agent, f"Invocation failed: {error}", status="blocked")
                detail = {"prompt": f"Task: {item['task']}\n{context}", "exit_code": -1,
                          "duration_seconds": 0.0, "stderr": str(error), "raw_output": ""}
            after = self.git.diff(worktree, base)
            response.files_changed = self.git.changed_files(worktree, base)
            if settings["read_only"] and (old_status != self.git.status(worktree) or before != after):
                response.status = "blocked"
                response.message += "\nRead-only worktree was modified; human inspection required."
            if agent == self.config["git"]["primary_implementation_agent"] and self.config["execution"]["tests"]:
                try:
                    response.tests_run = self.command_policy.run_tests(worktree)
                except Exception as error:
                    response.tests_run = [{"error": str(error)}]
            self._record(task, response, detail, before, after)
            if self.adapter_factory is None:
                try:
                    self.store.save_quota(agent, collect_quota(agent, settings["command"], self.root))
                except (OSError, ValueError, TimeoutError):
                    pass  # A usage lookup must never turn a completed agent response into a failed turn.
            task["turn_count"] += 1
            if self.store.get_task(task_id)["status"] == "stopped":
                task["status"] = "stopped"
                break
            if detail["exit_code"] != 0:
                task["queue"].insert(0, item)
                task["status"] = "failed"
                break
            if response.status in {"blocked", "disagree"}:
                task["status"] = "awaiting_human"
                break
            self._delegate(task, response, item["round"])
            self.store.save_task(task)
        if task["status"] == "running":
            if task["queue"]:
                task["status"] = "awaiting_human"
            else:
                messages = self.store.messages(task_id)
                agent_messages = [m for m in messages if m["sender"] in self.config["agents"]]
                task["status"] = "complete" if agent_messages and all(m["metadata"].get("status") == "complete" for m in agent_messages[-3:]) else "review_ready"
        self.store.save_task(task)
        return task

    def _record(self, task: dict[str, Any], response: AgentResponse, detail: dict[str, Any], before: str, after: str) -> None:
        delegation = {"to": response.requested_agent, "task": response.requested_task} if response.requested_agent else {}
        self.store.add_message(task["id"], response.agent, response.requested_agent or "all", response.message,
                               delegation, {"status": response.status, "files_changed": response.files_changed,
                                            "tests_run": response.tests_run, "usage": response.usage})
        record = {"id": uuid.uuid4().hex, "task_id": task["id"], "agent": response.agent,
                  "started_at": now(), "duration_seconds": detail["duration_seconds"],
                  "exit_code": detail["exit_code"], "prompt": detail["prompt"],
                  "raw_output": detail["raw_output"], "stderr": detail["stderr"],
                  "response": json.dumps(response.as_dict()), "diff_before": before, "diff_after": after}
        self.store.record_invocation(record)
        log_file = team_dir(self.root) / "logs" / f"{task['id']}.jsonl"
        log_file.parent.mkdir(exist_ok=True)
        with log_file.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")
        if response.tests_run:
            task["artifacts"].append({"agent": response.agent, "tests": response.tests_run})

    def _delegate(self, task: dict[str, Any], response: AgentResponse, current_round: int) -> None:
        if not self.config["collaboration"]["allow_agent_delegation"]:
            return
        target, request = response.requested_agent, response.requested_task
        if not target or not isinstance(request, str) or not request.strip():
            return
        recipients = [name for name, settings in self.config["agents"].items() if settings["enabled"]] if target == "all" else [target]
        for recipient in recipients:
            if recipient not in self.config["agents"] or not self.config["agents"][recipient]["enabled"]:
                continue
            if recipient == response.agent:
                continue
            signature = f"{response.agent}|{recipient}|{' '.join(request.lower().split())}"
            if signature in task["seen_delegations"]:
                continue
            task["seen_delegations"].append(signature)
            task["queue"].append({"agent": recipient, "task": request.strip(), "source": response.agent,
                                  "round": current_round + 1})

    def stop(self, task_id: str) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        task["status"] = "stopped"
        self.store.save_task(task)
        return task

    def guide(self, task_id: str, message: str, recipient: str = "all") -> dict[str, Any]:
        task = self.store.get_task(task_id)
        if task["status"] != "awaiting_human":
            raise ValueError("Task is not waiting for human guidance")
        return self.send_message(task_id, message, recipient)

    def send_message(self, task_id: str, message: str, recipient: str = "all") -> dict[str, Any]:
        task = self.store.get_task(task_id)
        if task["status"] not in {"awaiting_human", "review_ready", "complete", "stopped"}:
            raise ValueError("Wait for the current turn or retry the failed turn before messaging")
        if not isinstance(message, str) or not message.strip() or len(message) > 20_000:
            raise ValueError("Enter a message of at most 20,000 characters")
        enabled = [name for name, settings in self.config["agents"].items() if settings["enabled"]]
        if recipient != "all" and recipient not in enabled:
            raise ValueError("Choose an enabled agent or the whole team")
        recipients = enabled if recipient == "all" else [recipient]
        next_round = task["current_round"] + 1
        task["max_rounds"] = max(task["max_rounds"], next_round,
                                 *(item["round"] for item in task["queue"]))
        task["git_state"]["extra_turns"] = task["git_state"].get("extra_turns", 0) + len(recipients)
        task["queue"] = ([{"agent": name, "task": message.strip(), "source": "human", "round": next_round}
                          for name in recipients] + task["queue"])
        task["status"] = "running"
        self.store.add_message(task_id, "human", recipient, message.strip())
        self.store.save_task(task)
        return task

    def retry_failed(self, task_id: str) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        if task["status"] not in {"failed", "awaiting_human"}:
            raise ValueError("Task is not stopped on a failed CLI invocation")
        if task["git_state"].pop("setup_failure", False):
            if not task["queue"]:
                raise ValueError("No queued turn to retry")
            task["status"] = "running"
            self.store.save_task(task)
            return self.resume(task_id)
        invocation = self.store.db.execute(
            "SELECT agent, exit_code FROM invocations WHERE task_id=? ORDER BY rowid DESC LIMIT 1", (task_id,)
        ).fetchone()
        if invocation is None:
            if not task["queue"]:
                raise ValueError("No queued turn to retry")
            task["status"] = "running"
            self.store.save_task(task)
            return self.resume(task_id)
        if invocation["exit_code"] == 0:
            raise ValueError("Most recent invocation was not a CLI failure")
        if not task["queue"] or task["queue"][0]["agent"] != invocation["agent"]:
            task["queue"].insert(0, {"agent": invocation["agent"], "task": task["original_prompt"],
                                      "source": "retry", "round": task["current_round"]})
        task["status"] = "running"
        self.store.save_task(task)
        return self.resume(task_id)
