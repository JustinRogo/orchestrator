from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class AgentResponse:
    agent: str
    message: str
    requested_agent: str | None = None
    requested_task: str | None = None
    files_changed: list[str] = field(default_factory=list)
    tests_run: list[Any] = field(default_factory=list)
    status: str = "working"
    raw_output: str = ""
    usage: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_delegation(message: str) -> tuple[str | None, str | None]:
    match = re.search(r"(?im)^\s*@(codex|claude|gemini|all)\s*[:,-]?\s+(.+)$", message)
    return (match.group(1).lower(), match.group(2).strip()) if match else (None, None)


def _structured(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text.strip())
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def filtered_environment(allowed: list[str], source: dict[str, str] | None = None) -> dict[str, str]:
    names = {name.upper() for name in allowed}
    environment = os.environ if source is None else source
    return {key: value for key, value in environment.items() if key.upper() in names}


def parse_response(agent: str, raw: str, output_format: str) -> AgentResponse:
    payload: Any = None
    usage: dict[str, Any] = {}
    if output_format == "codex-jsonl":
        for line in raw.splitlines():
            event = _structured(line)
            if not event:
                continue
            if event.get("type") == "item.completed" and event.get("item", {}).get("type") == "agent_message":
                payload = event["item"].get("text", "")
            if event.get("type") == "turn.completed":
                usage = event.get("usage", {})
    elif output_format == "antigravity-json":
        envelope = _structured(raw)
        if envelope is None:
            return AgentResponse(agent, "Invalid Antigravity JSON response", status="blocked", raw_output=raw)
        usage = envelope.get("usage") or {}
        run_status = str(envelope.get("status", "")).upper()
        if run_status != "SUCCESS":
            reason = envelope.get("error") or envelope.get("response") or f"run status {run_status or 'missing'}"
            return AgentResponse(agent, f"Antigravity run failed: {reason}", status="blocked", raw_output=raw, usage=usage)
        payload = envelope.get("structured_output") or envelope.get("response", "")
        if not payload:
            return AgentResponse(agent, "Antigravity returned no response", status="blocked", raw_output=raw, usage=usage)
    else:
        payload = _structured(raw)
        if isinstance(payload, dict):
            usage = payload.get("usage") or payload.get("stats") or {}
            if not ("delegate_to" in payload or "requested_agent" in payload or "status" in payload):
                payload = payload.get("result") or payload.get("response") or payload.get("message") or payload
    if isinstance(payload, dict):
        data = payload
    else:
        text = str(payload) if payload is not None else raw.strip()
        data = _structured(text) or {"message": text}
    message = str(data.get("message", "")).strip()
    if not message:
        message = raw.strip()[-4000:]
    target = data.get("delegate_to") or data.get("requested_agent")
    request = data.get("delegated_task") or data.get("requested_task")
    if not target or not request:
        mention_target, mention_task = parse_delegation(message)
        target = target or mention_target
        request = request or mention_task
    status = str(data.get("status", "working")).lower()
    if status not in {"working", "complete", "blocked", "disagree"}:
        status = "working"
    return AgentResponse(agent, message, target, request, status=status, raw_output=raw, usage=usage)


class CLIAdapter:
    def __init__(self, name: str, settings: dict[str, Any], allowed_environment: list[str], timeout: int):
        self.name, self.settings = name, settings
        self.allowed_environment, self.timeout = allowed_environment, timeout

    def run(self, task: str, context: str, history: list[dict[str, Any]], worktree: Path) -> tuple[AgentResponse, dict[str, Any]]:
        executable = shutil.which(self.settings["command"])
        if not executable:
            raise RuntimeError(f"{self.name} CLI not found: {self.settings['command']}")
        prompt = (f"Role: {self.settings['role']}\nTask: {task}\n\n{context}\n\n"
                  "Reply with a JSON object containing message and status (working, complete, blocked, disagree). "
                  "For a specific request to another agent, include delegate_to and delegated_task. "
                  "Do not run destructive Git commands or access files outside this worktree.\n")
        if self.settings["read_only"]:
            prompt += "You are a read-only reviewer. Do not edit files or run modifying commands.\n"
        args = [part.replace("{prompt}", prompt).replace("{worktree}", str(worktree)) for part in self.settings["args"]]
        if self.settings["prompt_mode"] == "argument" and not any("{prompt}" in arg for arg in self.settings["args"]):
            raise ValueError(f"{self.name}: argument mode requires {{prompt}} in args")
        env = filtered_environment(self.allowed_environment)
        start = time.monotonic()
        result = subprocess.run([executable, *args], input=prompt if self.settings["prompt_mode"] == "stdin" else None,
                                cwd=worktree, env=env, text=True, capture_output=True, timeout=self.timeout, shell=False)
        elapsed = time.monotonic() - start
        response = parse_response(self.name, result.stdout, self.settings["format"])
        detail = {"prompt": prompt, "exit_code": result.returncode, "duration_seconds": elapsed,
                  "stderr": result.stderr, "raw_output": result.stdout}
        if result.returncode:
            response.status = "blocked"
            reason = result.stderr.strip()[-1000:] or response.message
            response.message = f"CLI exited {result.returncode}: {reason}"
        return response, detail


class CodexAdapter(CLIAdapter):
    pass


class ClaudeAdapter(CLIAdapter):
    pass


class AntigravityAdapter(CLIAdapter):
    pass


ADAPTERS = {"codex": CodexAdapter, "claude": ClaudeAdapter, "gemini": AntigravityAdapter}
