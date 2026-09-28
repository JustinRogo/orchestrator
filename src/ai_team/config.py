from __future__ import annotations

import copy
import glob
import json
import math
import os
import shutil
from pathlib import Path
from typing import Any

import yaml


AGY_RESPONSE_SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "message": {"type": "string"},
        "status": {"type": "string", "enum": ["working", "complete", "blocked", "disagree"]},
        "delegate_to": {"type": "string", "enum": ["codex", "claude", "gemini", "all"]},
        "delegated_task": {"type": "string"},
    },
    "required": ["message", "status"],
})


DEFAULT_CONFIG: dict[str, Any] = {
    "agents": {
        "codex": {"enabled": True, "role": "implementation", "read_only": False,
                  "command": "codex", "args": ["exec", "--sandbox", "workspace-write", "--json", "-"],
                  "format": "codex-jsonl", "prompt_mode": "stdin"},
        "claude": {"enabled": True, "role": "architecture and code review", "read_only": True,
                   "command": "claude", "args": ["-p", "--output-format", "json", "--permission-mode", "plan"],
                   "format": "claude-json", "prompt_mode": "stdin"},
        "gemini": {"enabled": True, "role": "independent QA", "read_only": True,
                   "command": "agy", "args": ["-p", "{prompt}", "--output-format", "json", "--json-schema", AGY_RESPONSE_SCHEMA, "--mode", "plan"],
                   "format": "antigravity-json", "prompt_mode": "argument"},
    },
    "collaboration": {"max_turns": 12, "max_rounds": 3, "allow_agent_delegation": True,
                      "recent_messages": 8, "timeout_seconds": 600},
    "limits": {"budget": {"tokens": None, "cost_usd": None}},
    "git": {"primary_implementation_agent": "codex", "auto_merge": False},
    "execution": {"tests": [], "allowed_environment": ["PATH", "PATHEXT", "SystemRoot", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "GEMINI_CLI_HOME", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS"]},
}


def team_dir(root: Path) -> Path:
    return root / ".ai-team"


def initialize(root: Path) -> Path:
    folder = team_dir(root)
    folder.mkdir(exist_ok=True)
    config_path = folder / "config.yaml"
    if not config_path.exists():
        config_path.write_text(yaml.safe_dump(DEFAULT_CONFIG, sort_keys=False), encoding="utf-8")
    for name, content in {
        "project.md": "# Project context\n\nAdd stable project instructions here.\n",
        "architecture.md": "# Architecture\n\nAdd architectural notes here.\n",
        "decisions.md": "# Decisions\n\nRecord human decisions here.\n",
        "agents.yaml": yaml.safe_dump({key: {"role": value["role"]} for key, value in DEFAULT_CONFIG["agents"].items()}, sort_keys=False),
    }.items():
        path = folder / name
        if not path.exists():
            path.write_text(content, encoding="utf-8")
    for name in ("tasks", "handoffs", "reviews", "logs", "worktrees"):
        (folder / name).mkdir(exist_ok=True)
    return folder


def load(root: Path) -> dict[str, Any]:
    path = team_dir(root) / "config.yaml"
    if not path.exists():
        raise ValueError("Run 'ai-team init' first.")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("config.yaml must contain a mapping")
    config = copy.deepcopy(DEFAULT_CONFIG)
    for section, value in data.items():
        if section not in config or not isinstance(value, dict):
            raise ValueError(f"Unknown or invalid config section: {section}")
        if section == "agents":
            for agent, options in value.items():
                if agent not in config["agents"] or not isinstance(options, dict):
                    raise ValueError(f"Unknown or invalid agent: {agent}")
                config["agents"][agent].update(options)
        elif section == "limits":
            if set(value) != {"budget"} or not isinstance(value["budget"], dict) or set(value["budget"]) - {"tokens", "cost_usd"}:
                raise ValueError("limits must contain a budget with tokens and/or cost_usd")
            config["limits"]["budget"].update(value["budget"])
        else:
            config[section].update(value)
    if config["git"]["auto_merge"]:
        raise ValueError("Automatic merging is unavailable in the MVP")
    for key in ("max_turns", "max_rounds", "recent_messages", "timeout_seconds"):
        if not isinstance(config["collaboration"][key], int) or config["collaboration"][key] < 1:
            raise ValueError(f"collaboration.{key} must be a positive integer")
    budget = config["limits"].get("budget")
    if not isinstance(budget, dict) or set(budget) != {"tokens", "cost_usd"}:
        raise ValueError("limits.budget must contain tokens and cost_usd")
    tokens, cost = budget["tokens"], budget["cost_usd"]
    if tokens is not None and (type(tokens) is not int or tokens <= 0):
        raise ValueError("limits.budget.tokens must be a positive integer or null")
    if cost is not None and (type(cost) not in (int, float) or not math.isfinite(cost) or cost <= 0):
        raise ValueError("limits.budget.cost_usd must be a positive finite number or null")
    for agent, options in config["agents"].items():
        if not isinstance(options["args"], list) or not all(isinstance(arg, str) for arg in options["args"]):
            raise ValueError(f"agents.{agent}.args must be a string list")
    return config


# Standard install locations for CLIs whose installers do not always update PATH.
# Codex installs into a hashed directory that changes on each update.
KNOWN_LOCATIONS: dict[str, list[str]] = {
    "codex": ["$LOCALAPPDATA/OpenAI/Codex/bin/*/codex.exe"],
    "claude": ["~/.local/bin/claude.exe", "~/.local/bin/claude", "~/.claude/local/claude"],
    "agy": ["$LOCALAPPDATA/agy/bin/agy.exe", "~/.local/bin/agy"],
}


def resolve_command(command: str) -> str | None:
    """Find an agent executable on PATH, then in its installer's standard location.

    A configured absolute path that no longer exists (for example, after a Codex update)
    falls back to the standard location for that executable's name.
    """
    found = shutil.which(command)
    if found:
        return found
    candidates: list[Path] = []
    for pattern in KNOWN_LOCATIONS.get(Path(command).stem.lower(), []):
        candidates += [Path(match) for match in glob.glob(os.path.expandvars(os.path.expanduser(pattern)))]
    candidates = [path for path in candidates if path.is_file()]
    return str(max(candidates, key=lambda path: path.stat().st_mtime)) if candidates else None


def available_agents(config: dict[str, Any]) -> dict[str, bool]:
    return {name: bool(resolve_command(options["command"])) for name, options in config["agents"].items() if options["enabled"]}
