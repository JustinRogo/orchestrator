from __future__ import annotations

import copy
import shutil
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG: dict[str, Any] = {
    "agents": {
        "codex": {"enabled": True, "role": "implementation", "read_only": False,
                  "command": "codex", "args": ["exec", "--sandbox", "workspace-write", "--json", "-"],
                  "format": "codex-jsonl", "prompt_mode": "stdin"},
        "claude": {"enabled": True, "role": "architecture and code review", "read_only": True,
                   "command": "claude", "args": ["-p", "--output-format", "json", "--permission-mode", "plan"],
                   "format": "claude-json", "prompt_mode": "stdin"},
        "gemini": {"enabled": True, "role": "independent QA", "read_only": True,
                   "command": "gemini", "args": ["-p", "{prompt}", "--output-format", "json", "--approval-mode", "plan"],
                   "format": "gemini-json", "prompt_mode": "argument"},
    },
    "collaboration": {"max_turns": 12, "max_rounds": 3, "allow_agent_delegation": True,
                      "recent_messages": 8, "timeout_seconds": 600},
    "git": {"primary_implementation_agent": "codex", "auto_merge": False},
    "execution": {"tests": [], "allowed_environment": ["PATH", "PATHEXT", "SystemRoot", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "GEMINI_CLI_HOME"]},
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
        else:
            config[section].update(value)
    if config["git"]["auto_merge"]:
        raise ValueError("Automatic merging is unavailable in the MVP")
    for key in ("max_turns", "max_rounds", "recent_messages", "timeout_seconds"):
        if not isinstance(config["collaboration"][key], int) or config["collaboration"][key] < 1:
            raise ValueError(f"collaboration.{key} must be a positive integer")
    for agent, options in config["agents"].items():
        if not isinstance(options["args"], list) or not all(isinstance(arg, str) for arg in options["args"]):
            raise ValueError(f"agents.{agent}.args must be a string list")
    return config


def available_agents(config: dict[str, Any]) -> dict[str, bool]:
    return {name: bool(shutil.which(options["command"])) for name, options in config["agents"].items() if options["enabled"]}
