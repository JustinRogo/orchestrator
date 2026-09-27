from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, original_prompt TEXT NOT NULL,
                status TEXT NOT NULL, created_at TEXT NOT NULL, current_round INTEGER NOT NULL,
                max_rounds INTEGER NOT NULL, turn_count INTEGER NOT NULL,
                roles TEXT NOT NULL, queue TEXT NOT NULL, seen_delegations TEXT NOT NULL,
                git_state TEXT NOT NULL, artifacts TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                sender TEXT NOT NULL, recipient TEXT, content TEXT NOT NULL,
                timestamp TEXT NOT NULL, delegation TEXT NOT NULL, metadata TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS invocations (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, agent TEXT NOT NULL,
                started_at TEXT NOT NULL, duration_seconds REAL NOT NULL,
                exit_code INTEGER NOT NULL, prompt TEXT NOT NULL, raw_output TEXT NOT NULL,
                stderr TEXT NOT NULL, response TEXT NOT NULL, diff_before TEXT NOT NULL,
                diff_after TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS agent_quota (
                agent TEXT PRIMARY KEY, snapshot TEXT NOT NULL);
        """)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def create_task(self, prompt: str, config: dict[str, Any], roles: dict[str, str] | None = None,
                    recipient: str = "all") -> dict[str, Any]:
        task_id = uuid.uuid4().hex[:12]
        agents = [name for name, entry in config["agents"].items() if entry["enabled"]]
        if not agents:
            raise ValueError("No agents are enabled")
        if recipient != "all":
            if recipient not in agents:
                raise ValueError("Choose an enabled agent or the whole team")
            agents = [recipient]
        task = {"id": task_id, "title": prompt.strip().splitlines()[0][:100], "original_prompt": prompt,
                "status": "running", "created_at": now(), "current_round": 1,
                "max_rounds": config["collaboration"]["max_rounds"], "turn_count": 0,
                "roles": roles or {}, "queue": [{"agent": name, "task": prompt, "source": "human", "round": 1} for name in agents],
                "seen_delegations": [], "git_state": {}, "artifacts": []}
        self.save_task(task)
        self.add_message(task_id, "human", recipient, prompt)
        return task

    def last_agent_usage(self) -> dict[str, int | None]:
        usage: dict[str, int | None] = {}
        for row in self.db.execute("SELECT agent, response FROM invocations ORDER BY rowid DESC"):
            agent = row["agent"]
            if agent in usage:
                continue
            try:
                values = json.loads(row["response"]).get("usage") or {}
                count = values.get("total_tokens")
                if not isinstance(count, (int, float)):
                    parts = (values.get(key) for key in (
                        "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"
                    ))
                    count = sum(part for part in parts if isinstance(part, (int, float)))
                usage[agent] = int(count) if count else None
            except (ValueError, TypeError, AttributeError):
                usage[agent] = None
        return usage

    def save_quota(self, agent: str, snapshot: dict[str, Any]) -> None:
        self.db.execute("INSERT INTO agent_quota VALUES (?, ?) ON CONFLICT(agent) DO UPDATE SET snapshot=excluded.snapshot",
                        (agent, json.dumps(snapshot)))
        self.db.commit()

    def agent_quotas(self) -> dict[str, dict[str, Any]]:
        return {row["agent"]: json.loads(row["snapshot"])
                for row in self.db.execute("SELECT agent, snapshot FROM agent_quota")}

    def save_task(self, task: dict[str, Any]) -> None:
        row = dict(task)
        for key in ("roles", "queue", "seen_delegations", "git_state", "artifacts"):
            row[key] = json.dumps(row[key])
        self.db.execute("""INSERT INTO tasks VALUES (:id,:title,:original_prompt,:status,:created_at,
                         :current_round,:max_rounds,:turn_count,:roles,:queue,:seen_delegations,:git_state,:artifacts)
                         ON CONFLICT(id) DO UPDATE SET status=excluded.status,current_round=excluded.current_round,
                         max_rounds=excluded.max_rounds,turn_count=excluded.turn_count,roles=excluded.roles,queue=excluded.queue,
                         seen_delegations=excluded.seen_delegations,git_state=excluded.git_state,artifacts=excluded.artifacts""", row)
        self.db.commit()

    def get_task(self, task_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown task: {task_id}")
        task = dict(row)
        for key in ("roles", "queue", "seen_delegations", "git_state", "artifacts"):
            task[key] = json.loads(task[key])
        return task

    def list_tasks(self) -> list[dict[str, Any]]:
        return [self.get_task(row["id"]) for row in self.db.execute("SELECT id FROM tasks ORDER BY created_at DESC")]

    def add_message(self, task_id: str, sender: str, recipient: str | None, content: str,
                    delegation: dict[str, Any] | None = None, metadata: dict[str, Any] | None = None) -> None:
        self.db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex, task_id, sender, recipient, content, now(),
                         json.dumps(delegation or {}), json.dumps(metadata or {})))
        self.db.commit()

    def messages(self, task_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM messages WHERE task_id=? ORDER BY timestamp,rowid", (task_id,)).fetchall()
        return [{**dict(row), "delegation": json.loads(row["delegation"]), "metadata": json.loads(row["metadata"])} for row in rows]

    def record_invocation(self, data: dict[str, Any]) -> None:
        self.db.execute("""INSERT INTO invocations VALUES (:id,:task_id,:agent,:started_at,:duration_seconds,
                         :exit_code,:prompt,:raw_output,:stderr,:response,:diff_before,:diff_after)""", data)
        self.db.commit()
