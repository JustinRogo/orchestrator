from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import available_agents, initialize, load
from .coordinator import Coordinator


def parser() -> argparse.ArgumentParser:
    app = argparse.ArgumentParser(prog="ai-team", description="Local multi-agent development coordinator")
    app.add_argument("--project", type=Path, default=Path.cwd(), help="Git repository root")
    sub = app.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    ui = sub.add_parser("ui", help="Open the local browser interface")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--no-browser", action="store_true")
    start = sub.add_parser("start")
    start.add_argument("prompt")
    start.add_argument("--role", action="append", default=[], metavar="AGENT=ROLE")
    sub.add_parser("status")
    for name in ("chat", "resume", "retry", "diff", "review", "stop", "cleanup"):
        command = sub.add_parser(name)
        command.add_argument("task_id")
    return app


def show_chat(coordinator: Coordinator, task_id: str) -> None:
    task = coordinator.store.get_task(task_id)
    print(f"Task {task_id} [{task['status']}] - {task['title']}")
    for message in coordinator.store.messages(task_id):
        print(f"\n[{message['sender'].upper()} -> {message['recipient'].upper() if message['recipient'] else 'ALL'}]\n{message['content']}")
    if task["status"] == "awaiting_human":
        print("\nHUMAN DECISION REQUIRED: inspect the conversation and worktree diffs.")
    elif task["status"] == "failed":
        print(f"\nCLI invocation failed. Retry with: ai-team retry {task_id}")


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
    args = parser().parse_args(argv)
    root = args.project.resolve()
    try:
        if args.command == "init":
            path = initialize(root)
            config = load(root)
            print(f"Initialized {path}")
            for agent, found in available_agents(config).items():
                print(f"{agent} ({config['agents'][agent]['command']}): {'found' if found else 'missing; configure or install before start'}")
            return 0
        config = load(root)
        if args.command == "ui":
            from .server import run_ui
            run_ui(root, args.port, not args.no_browser)
            return 0
        coordinator = Coordinator(root, config)
        if args.command == "start":
            roles = {}
            for entry in args.role:
                name, separator, role = entry.partition("=")
                if not separator or name not in config["agents"] or not role.strip():
                    raise ValueError("--role must be AGENT=ROLE for a configured agent")
                roles[name] = role.strip()
            missing = [name for name, found in available_agents(config).items() if not found]
            if missing:
                labels = [f"{name} ({config['agents'][name]['command']})" for name in missing]
                raise ValueError(f"Required CLIs missing: {', '.join(labels)}. Edit .ai-team/config.yaml to disable or configure them.")
            task = coordinator.start(args.prompt, roles)
            show_chat(coordinator, task["id"])
        elif args.command == "status":
            for task in coordinator.store.list_tasks():
                print(f"{task['id']}  {task['status']:<15} {task['turn_count']} turns  {task['title']}")
        elif args.command == "chat":
            show_chat(coordinator, args.task_id)
        elif args.command == "resume":
            coordinator.resume(args.task_id)
            show_chat(coordinator, args.task_id)
        elif args.command == "retry":
            coordinator.retry_failed(args.task_id)
            show_chat(coordinator, args.task_id)
        elif args.command == "diff":
            task = coordinator.store.get_task(args.task_id)
            for agent in config["agents"]:
                path = coordinator.git.path(task["id"], agent)
                if path.exists():
                    print(f"\n[{agent.upper()}] {path}\n{coordinator.git.diff(path, task['git_state'].get('base', 'HEAD'))}")
        elif args.command == "review":
            task = coordinator.store.get_task(args.task_id)
            show_chat(coordinator, task["id"])
            for agent in config["agents"]:
                path = coordinator.git.path(task["id"], agent)
                if path.exists():
                    changed = coordinator.git.changed_files(path, task["git_state"].get("base", "HEAD"))
                    print(f"\n{agent}: {', '.join(changed) or 'no changed files'}")
            print("\nNo merge command is available in this MVP.")
        elif args.command == "stop":
            task = coordinator.stop(args.task_id)
            print(f"Stopped {task['id']}")
        elif args.command == "cleanup":
            task = coordinator.store.get_task(args.task_id)
            if task["status"] == "running":
                raise ValueError("Stop the task before cleanup")
            for agent in config["agents"]:
                coordinator.git.cleanup(task["id"], agent)
            print(f"Removed clean worktrees for {task['id']}; task history is retained")
        return 0
    except (ValueError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
