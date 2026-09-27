"""Read plan limits through each CLI's local, zero-token usage command."""
from __future__ import annotations

import json
import queue
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import resolve_command
from .process import console_flags


def _snapshot(windows: list[dict[str, Any]], source: str) -> dict[str, Any]:
    return {"checked_at": datetime.now(timezone.utc).isoformat(), "source": source,
            "windows": windows}


def _window(label: str, remaining: float, reset: str | int | None = None,
            group: str | None = None) -> dict[str, Any]:
    if not 0 <= remaining <= 100:
        raise ValueError("Invalid remaining quota percentage")
    result = {"label": label, "remaining_percent": round(remaining)}
    if reset is not None:
        result["resets_at"] = reset
    if group:
        result["group"] = group
    return result


def parse_codex(payload: dict[str, Any]) -> dict[str, Any]:
    limits = (payload.get("rateLimitsByLimitId") or {}).get("codex") or payload.get("rateLimits") or {}
    windows = []
    for key, fallback in (("primary", "5h"), ("secondary", "week")):
        value = limits.get(key) or {}
        used = value.get("usedPercent")
        if not isinstance(used, (int, float)):
            continue
        minutes = value.get("windowDurationMins")
        label = "5h" if minutes == 300 else "week" if minutes == 10080 else fallback
        windows.append(_window(label, 100 - used, value.get("resetsAt")))
    if not windows:
        raise ValueError("Codex did not provide rate limits")
    return _snapshot(windows, "Codex account limits")


def parse_claude(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("is_error"):
        raise ValueError("Claude usage command failed")
    result = payload.get("result") or ""
    windows = []
    for line in result.splitlines():
        match = re.search(r"^(Current session|Current week[^:]*):\s*(\d+(?:\.\d+)?)% used(?:\s*[·-]\s*resets\s+(.+))?", line, re.I)
        if match:
            label = "session" if match.group(1).lower() == "current session" else "week"
            windows.append(_window(label, 100 - float(match.group(2)), match.group(3)))
    if not windows:
        raise ValueError("Claude did not provide plan limits")
    return _snapshot(windows, "Claude /usage")


def parse_antigravity(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("status") != "SUCCESS":
        raise ValueError("Antigravity usage command failed")
    groups = ((payload.get("command") or {}).get("data") or {}).get("groups") or []
    windows = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        for bucket in group.get("buckets") or []:
            if not isinstance(bucket, dict):
                continue
            fraction = bucket.get("remaining_fraction")
            if not isinstance(fraction, (int, float)):
                continue
            label = "5h" if bucket.get("window") == "5h" else "week" if bucket.get("window") == "weekly" else str(bucket.get("name", "quota"))
            windows.append(_window(label, fraction * 100, bucket.get("reset_time"), group.get("name")))
    if not windows:
        raise ValueError("Antigravity did not provide model limits")
    return _snapshot(windows, "Antigravity /usage")


def _codex(command: str, root: Path) -> dict[str, Any]:
    process = subprocess.Popen([command, "app-server"], cwd=root, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               text=True, encoding="utf-8", errors="replace", creationflags=console_flags())
    lines: queue.Queue[str] = queue.Queue()
    def read_lines() -> None:
        for line in process.stdout:
            lines.put(line)

    reader = threading.Thread(target=read_lines, daemon=True)
    reader.start()
    try:
        requests = [
            {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "ai-team", "version": "0.1"}}},
            {"method": "initialized"},
            {"id": 2, "method": "account/rateLimits/read", "params": {"excludeResetCreditDetails": True}},
        ]
        for request in requests:
            process.stdin.write(json.dumps(request) + "\n")
            process.stdin.flush()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                response = json.loads(lines.get(timeout=max(0.01, deadline - time.monotonic())))
            except (ValueError, TypeError):
                continue
            except queue.Empty as error:
                raise TimeoutError("Codex usage query timed out") from error
            if response.get("id") == 2:
                if response.get("error"):
                    raise ValueError("Codex usage query failed")
                return parse_codex(response.get("result") or {})
        raise TimeoutError("Codex usage query timed out")
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def collect(agent: str, command: str, root: Path) -> dict[str, Any]:
    command = resolve_command(command) or command
    if agent == "codex":
        return _codex(command, root)
    if agent not in {"claude", "gemini"}:
        raise ValueError(f"Unknown agent: {agent}")
    try:
        result = subprocess.run([command, "-p", "/usage", "--output-format", "json"], cwd=root,
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
                                creationflags=console_flags())
    except subprocess.TimeoutExpired as error:
        raise TimeoutError(f"{agent} usage query timed out") from error
    if result.returncode:
        raise ValueError(f"{agent} usage command exited {result.returncode}")
    payload = json.loads(result.stdout)
    return parse_claude(payload) if agent == "claude" else parse_antigravity(payload)
