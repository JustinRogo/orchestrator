from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from .config import available_agents, initialize, load, team_dir
from .coordinator import Coordinator
from .process import console_flags


def parser() -> argparse.ArgumentParser:
    app = argparse.ArgumentParser(prog="ai-team", description="Local multi-agent development coordinator")
    app.add_argument("--project", type=Path, default=Path.cwd(), help="Git repository root")
    sub = app.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    ui = sub.add_parser("ui", help="Open the local browser interface")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--no-browser", action="store_true")
    sub.add_parser("shortcut", help="Create a desktop shortcut that opens the interface for this project")
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


def project_root(path: Path) -> Path:
    """Return the repository top level so commands work from any subdirectory."""
    try:
        result = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=path, capture_output=True,
                                text=True, check=False, creationflags=console_flags())
    except OSError as error:
        raise ValueError(f"Cannot open project {path}: {error}") from error
    if result.returncode:
        raise ValueError(f"{path} is not inside a Git repository. Pass --project PATH to choose one.")
    return Path(result.stdout.strip()).resolve()


def create_shortcut(root: Path) -> Path:
    if os.name != "nt":
        raise ValueError("Desktop shortcuts are only supported on Windows; run 'ai-team ui' instead")
    python = Path(sys.executable)
    windowless = python.with_name("pythonw.exe")
    # `-m` imports from the working directory, so the shortcut also works without a pip install.
    package_parent = Path(__file__).resolve().parent.parent
    script = (
        "$path = Join-Path ([Environment]::GetFolderPath('Desktop')) $env:AI_TEAM_LINK;"
        "$link = (New-Object -ComObject WScript.Shell).CreateShortcut($path);"
        "$link.TargetPath = $env:AI_TEAM_TARGET; $link.Arguments = $env:AI_TEAM_ARGS;"
        "$link.WorkingDirectory = $env:AI_TEAM_WORKDIR; $link.Description = $env:AI_TEAM_DESCRIPTION;"
        "$link.Save(); Write-Output $path"
    )
    environment = {**os.environ,
                   "AI_TEAM_LINK": f"AI Team - {root.name}.lnk",
                   "AI_TEAM_TARGET": str(windowless if windowless.exists() else python),
                   "AI_TEAM_ARGS": f'-m ai_team --project "{root}" ui',
                   "AI_TEAM_WORKDIR": str(package_parent),
                   "AI_TEAM_DESCRIPTION": f"Open the AI Team dashboard for {root}"}
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                            env=environment, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(f"Could not create shortcut: {result.stderr.strip()}")
    return Path(result.stdout.strip())


def report_error(message: str) -> None:
    if sys.stderr is not None:
        print(f"error: {message}", file=sys.stderr)
    elif os.name == "nt":
        # A windowless launch has no console, so show startup errors in a dialog.
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "AI Team", 0x10)


def main(argv: list[str] | None = None) -> int:
    # pythonw (the desktop shortcut) runs without standard streams.
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            stream.reconfigure(errors="replace")
    args = parser().parse_args(argv)
    try:
        root = project_root(args.project.resolve())
        if args.command == "init":
            path = initialize(root)
            config = load(root)
            print(f"Initialized {path}")
            for agent, found in available_agents(config).items():
                print(f"{agent} ({config['agents'][agent]['command']}): {'found' if found else 'missing; configure or install before start'}")
            return 0
        if args.command == "shortcut":
            path = create_shortcut(root)
            print(f"Created {path}")
            print(f"Double-click it to open the dashboard for {root}; right-click to pin it to Start or the taskbar.")
            return 0
        if args.command == "ui" and not (team_dir(root) / "config.yaml").exists():
            print(f"Initialized {initialize(root)}")
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
    except (ValueError, RuntimeError, OSError) as error:
        report_error(str(error))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
