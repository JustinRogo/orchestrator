"""Task-level roles and their execution permissions."""
from __future__ import annotations

from typing import Any

ROLE_LABELS = {
    "implementation": "Implementor",
    "q&a": "Q&A",
    "review": "Reviewer",
    "qa": "QA",
}


def validate_roles(roles: dict[str, str] | None, agents: dict[str, Any]) -> dict[str, str]:
    if roles is None:
        return {}
    if not isinstance(roles, dict) or any(
        name not in agents or not agents[name]["enabled"] or
        not isinstance(role, str) or role not in ROLE_LABELS
        for name, role in roles.items()
    ):
        raise ValueError("Choose an enabled agent and a role: implementation, q&a, review, or qa")
    if sum(role == "implementation" for role in roles.values()) > 1:
        raise ValueError("Only one agent can be the implementor")
    return roles


def implementation_agent(task: dict[str, Any], config: dict[str, Any]) -> str | None:
    roles = task["roles"]
    return next((name for name, role in roles.items() if role == "implementation"),
                config["git"]["primary_implementation_agent"]
                if not any(role in ROLE_LABELS for role in roles.values()) else None)


def _set_option(args: list[str], option: str, value: str, agent: str) -> None:
    positions = [index for index, arg in enumerate(args) if arg == option]
    if len(positions) != 1 or positions[0] + 1 >= len(args) or args[positions[0] + 1].startswith("-"):
        raise ValueError(f"agents.{agent}.args must contain exactly one {option} VALUE for task roles")
    args[positions[0] + 1] = value


def turn_settings(agent: str, task: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    settings = dict(config["agents"][agent])
    roles = task["roles"]
    if not any(role in ROLE_LABELS for role in roles.values()):
        return settings
    role = roles.get(agent) or ("implementation" if agent == implementation_agent(task, config) else "review")
    if role not in ROLE_LABELS:
        role = "review"
    settings["role"] = ROLE_LABELS[role]
    settings["read_only"] = role != "implementation"
    args = list(settings["args"])
    if agent == "codex":
        _set_option(args, "--sandbox", "workspace-write" if role == "implementation" else "read-only", agent)
    if agent == "claude":
        _set_option(args, "--permission-mode", "acceptEdits" if role == "implementation" else "plan", agent)
    if agent == "gemini":
        _set_option(args, "--mode", "accept-edits" if role == "implementation" else "plan", agent)
    settings["args"] = args
    return settings
