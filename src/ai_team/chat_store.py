"""Additive chat storage that does not construct a code coordinator."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

from .chat_output import sanitized
from .store import now


SCHEMA_VERSION = 5


class ChatStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA foreign_keys = ON")
            self._migrate()
        except BaseException:
            self.close()
            raise

    def _migrate(self) -> None:
        if self.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION:
            return
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError("This chat database was created by a newer version of AI Team.")
            if version == 0:
                self.db.execute("""CREATE TABLE chat_sessions (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    provider TEXT NOT NULL, model TEXT NOT NULL, system_prompt TEXT NOT NULL,
                    max_turns INTEGER NOT NULL, max_output_tokens INTEGER NOT NULL, timeout_seconds INTEGER NOT NULL)""")
                self.db.execute("""CREATE TABLE chat_turns (
                    id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES chat_sessions(id),
                    request_id TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL, prompt TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'complete', 'failed', 'interrupted')),
                    created_at TEXT NOT NULL, finished_at TEXT, owner TEXT NOT NULL, deadline REAL NOT NULL,
                    context_turns INTEGER NOT NULL, context_truncated INTEGER NOT NULL,
                    response TEXT, response_model TEXT, output_limited INTEGER,
                    input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER, error TEXT,
                    UNIQUE (session_id, request_id))""")
                self.db.execute("CREATE UNIQUE INDEX chat_pending_session ON chat_turns(session_id) WHERE status='pending'")
            if version < 2:
                self.db.execute("ALTER TABLE chat_sessions ADD COLUMN strategy TEXT NOT NULL DEFAULT 'single'")
                self.db.execute("ALTER TABLE chat_sessions ADD COLUMN strategy_options TEXT NOT NULL DEFAULT '{}'")
                self.db.execute("""CREATE TABLE chat_runs (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES chat_sessions(id),
                    request_id TEXT NOT NULL, owner TEXT, state TEXT NOT NULL, UNIQUE (session_id, request_id))""")
            if version < 3:
                self.db.execute("ALTER TABLE chat_turns ADD COLUMN cancelled_at TEXT")
            if version < 4:
                self.db.execute("""CREATE TABLE chat_teams (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE, configuration TEXT NOT NULL)""")
            if version < 5:
                self.db.execute("ALTER TABLE chat_sessions ADD COLUMN branch_source TEXT")
                self.db.execute("""CREATE TABLE chat_handoffs (
                    request_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES chat_sessions(id),
                    task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id), payload TEXT NOT NULL, created_at TEXT NOT NULL)""")
                self.db.execute("CREATE INDEX chat_handoffs_session ON chat_handoffs(session_id)")
            self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        self.db.close()

    def create_team(self, name: str, configuration: dict) -> dict | None:
        team_id = uuid.uuid4().hex
        inserted = self.db.execute("""INSERT INTO chat_teams (id, name, configuration) VALUES (?, ?, ?)
                                     ON CONFLICT(name) DO NOTHING""", (team_id, name, json.dumps(configuration)))
        return {"id": team_id, "name": name, **configuration} if inserted.rowcount else None

    def list_teams(self) -> list[dict]:
        return [{"id": row["id"], "name": row["name"], **json.loads(row["configuration"])}
                for row in self.db.execute("SELECT id, name, configuration FROM chat_teams ORDER BY name")]

    def create(self, provider: str, model: str, settings: dict, *, strategy: str = "single",
               strategy_options: dict | None = None, branch_source: dict | None = None, session_id: str | None = None) -> dict:
        stamp = now()
        title = f"Branch of {branch_source['title']}"[:110] if branch_source else "New conversation"
        session = {"id": session_id or uuid.uuid4().hex, "title": title, "provider": provider, "model": model,
                   "created_at": stamp, "updated_at": stamp, "strategy": strategy,
                   "strategy_options": strategy_options or {}, "branch_source": branch_source, **settings}
        self.db.execute("""INSERT INTO chat_sessions
                (id, title, created_at, updated_at, provider, model, system_prompt, max_turns, max_output_tokens,
                 timeout_seconds, strategy, strategy_options, branch_source)
                VALUES (:id, :title, :created_at, :updated_at, :provider, :model, :system_prompt,
                        :max_turns, :max_output_tokens, :timeout_seconds, :strategy, :strategy_options, :branch_source)""",
                        {**session, "strategy_options": json.dumps(session["strategy_options"]),
                         "branch_source": json.dumps(branch_source) if branch_source else None})
        return session

    def session(self, session_id: str) -> dict:
        row = self.db.execute("SELECT * FROM chat_sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise KeyError(session_id)
        return {**dict(row), "strategy_options": json.loads(row["strategy_options"]),
                "branch_source": json.loads(row["branch_source"]) if row["branch_source"] else None}

    def copy_turns(self, source_id: str, target_id: str, checkpoint: int) -> None:
        self.db.execute("""INSERT INTO chat_turns
            (session_id, request_id, provider, model, prompt, status, created_at, finished_at, owner, deadline,
             context_turns, context_truncated, response, response_model, output_limited, input_tokens, output_tokens,
             total_tokens, error, cancelled_at)
            SELECT ?, lower(hex(randomblob(16))), provider, model, prompt, status, created_at, finished_at, '', 0,
                   context_turns, context_truncated, response, response_model, output_limited, input_tokens, output_tokens,
                   total_tokens, error, cancelled_at
            FROM chat_turns WHERE session_id=? AND id<=? ORDER BY id""", (target_id, source_id, checkpoint))

    def handoffs(self, session_id: str) -> list[dict]:
        return [{"task_id": row["task_id"], "created_at": row["created_at"],
                 "title": json.loads(row["payload"])["brief"]["objective"].splitlines()[0][:100]}
                for row in self.db.execute("""SELECT task_id, created_at, payload FROM chat_handoffs
                                             WHERE session_id=? ORDER BY rowid DESC""", (session_id,))]

    def handoff_source(self, task_id: str) -> dict | None:
        row = self.db.execute("""SELECT s.id, s.title FROM chat_handoffs h
                                 JOIN chat_sessions s ON s.id=h.session_id WHERE h.task_id=?""", (task_id,)).fetchone()
        return dict(row) if row else None

    def linked_outputs(self, session_id: str, task_id: str) -> dict:
        link = self.db.execute("SELECT payload, created_at FROM chat_handoffs WHERE session_id=? AND task_id=?",
                               (session_id, task_id)).fetchone()
        if link is None:
            raise KeyError(task_id)
        task = self.db.execute("SELECT status, artifacts FROM tasks WHERE id=?", (task_id,)).fetchone()
        if task is None:
            raise KeyError(task_id)
        messages = self.db.execute("""SELECT id, sender AS agent, content, timestamp FROM messages
                                      WHERE task_id=? AND sender IN ('codex', 'claude', 'gemini')
                                      ORDER BY rowid DESC LIMIT 12""", (task_id,)).fetchall()
        invocations = self.db.execute("""SELECT agent, started_at, response, diff_after FROM invocations
                                        WHERE rowid IN (SELECT MAX(rowid) FROM invocations WHERE task_id=? GROUP BY agent)
                                        ORDER BY rowid""", (task_id,))
        updates = sanitized([dict(row) for row in reversed(messages)])
        for update in updates:
            update["truncated"] = len(update["content"]) > 8000
            update["content"] = update["content"][:8000]
        changes = {}
        for row in invocations:
            diff = sanitized(row["diff_after"])
            response = json.loads(row["response"])
            changes[row["agent"]] = {"files": sanitized(response.get("files_changed") or []), "diff": diff[:200_000],
                                     "truncated": len(diff) > 200_000, "timestamp": row["started_at"]}
        tests = [{"agent": artifact["agent"], "command": entry.get("command"), "exit_code": entry.get("exit_code"),
                  "error": entry.get("error")} for artifact in json.loads(task["artifacts"])[-16:]
                 for entry in artifact.get("tests", [])]
        return {"status": task["status"], "handoff": sanitized(json.loads(link["payload"])), "created_at": link["created_at"],
                "updates": updates, "changes": changes, "tests": sanitized(tests[-20:])}

    def list_sessions(self) -> list[dict]:
        return [dict(row) for row in self.db.execute("""SELECT id, title, provider, model, strategy, updated_at FROM chat_sessions
                                                       ORDER BY updated_at DESC, rowid DESC""")]

    def runs(self, session_id: str) -> list[dict]:
        return [json.loads(row["state"]) for row in self.db.execute(
            "SELECT state FROM chat_runs WHERE session_id=? ORDER BY rowid", (session_id,))]

    def run(self, session_id: str, run_id: str) -> dict:
        row = self.db.execute("SELECT state FROM chat_runs WHERE session_id=? AND id=?", (session_id, run_id)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return json.loads(row["state"])

    def save_run(self, run: dict) -> None:
        self.db.execute("""INSERT INTO chat_runs (id, session_id, request_id, owner, state) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET state=excluded.state, owner=excluded.owner""",
            (run["id"], run["session_id"], run["request_id"], run["owner"], json.dumps(run, ensure_ascii=False)))

    def owned_runs(self, owner: str) -> list[dict]:
        return [json.loads(row["state"]) for row in self.db.execute("SELECT state FROM chat_runs WHERE owner=?", (owner,))]

    def title_run(self, session_id: str, prompt: str, *, first: bool) -> None:
        stamp = now()
        if first:
            self.db.execute("UPDATE chat_sessions SET title=?, updated_at=? WHERE id=?",
                            (prompt.splitlines()[0][:90], stamp, session_id))
        else:
            self.db.execute("UPDATE chat_sessions SET updated_at=? WHERE id=?", (stamp, session_id))

    def turns(self, session_id: str) -> list[dict]:
        rows = self.db.execute("""SELECT id, provider, model, prompt,
                 CASE WHEN cancelled_at IS NOT NULL THEN 'cancelled' ELSE status END AS status, created_at, finished_at,
                 context_turns, context_truncated, response, response_model, output_limited, input_tokens,
                 output_tokens, total_tokens, error FROM chat_turns WHERE session_id=? ORDER BY id""", (session_id,))
        return [dict(row) for row in rows]

    def turn_claim(self, session_id: str, turn_id: int) -> sqlite3.Row:
        row = self.db.execute("SELECT status, owner, cancelled_at FROM chat_turns WHERE session_id=? AND id=?",
                              (session_id, turn_id)).fetchone()
        if row is None:
            raise KeyError(turn_id)
        return row

    def cancel_turn(self, turn_id: int, message: str) -> None:
        stamp = now()
        self.db.execute("UPDATE chat_turns SET status='interrupted', cancelled_at=?, finished_at=?, error=? WHERE id=?",
                        (stamp, stamp, message, turn_id))

    def find_request(self, session_id: str, request_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM chat_turns WHERE session_id=? AND request_id=?",
                               (session_id, request_id)).fetchone()

    def add_turn(self, session: dict, request_id: str, provider: str, model: str, prompt: str,
                 owner: str, context_turns: int, truncated: bool) -> int:
        stamp = now()
        cursor = self.db.execute("""INSERT INTO chat_turns
            (session_id, request_id, provider, model, prompt, status, created_at, owner, deadline,
             context_turns, context_truncated) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)""",
            (session["id"], request_id, provider, model, prompt, stamp, owner,
             time.time() + session["timeout_seconds"], context_turns, int(truncated)))
        self.db.execute("""UPDATE chat_sessions SET provider=?, model=?, updated_at=? WHERE id=?""",
                        (provider, model, stamp, session["id"]))
        if not self.db.execute("SELECT 1 FROM chat_turns WHERE session_id=? AND id<>? LIMIT 1",
                               (session["id"], cursor.lastrowid)).fetchone():
            self.db.execute("UPDATE chat_sessions SET title=? WHERE id=?", (prompt.splitlines()[0][:90], session["id"]))
        return cursor.lastrowid

    def finish_turn(self, turn_id: int, *, response: str | None = None, response_model: str | None = None,
                    output_limited: bool = False, input_tokens: int | None = None, output_tokens: int | None = None,
                    total_tokens: int | None = None, error: str | None = None) -> None:
        with self.db:
            self.expire_turns()
            self.db.execute("""UPDATE chat_turns SET status=?, finished_at=?, response=?, response_model=?,
                output_limited=?, input_tokens=?, output_tokens=?, total_tokens=?, error=? WHERE id=? AND status='pending'""",
                ("failed" if error else "complete", now(), response, response_model, int(output_limited), input_tokens,
                 output_tokens, total_tokens, error, turn_id))

    def expire_turns(self) -> None:
        self.db.execute("""UPDATE chat_turns SET status='interrupted', finished_at=?,
            error='The turn has timed out or the server was interrupted. Check the provider before resending.'
            WHERE status='pending' AND deadline<=?""", (now(), time.time()))

    def interrupt(self, owner: str) -> None:
        with self.db:
            self.db.execute("""UPDATE chat_turns SET status='interrupted', finished_at=?,
                error='The server stopped before the response was saved. Check the provider before resending.'
                WHERE status='pending' AND owner=?""", (now(), owner))
