from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .process import console_flags


class GitWorkspaceManager:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.base = self.root / ".ai-team" / "worktrees"
        self._git("rev-parse", "--show-toplevel")

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(["git", *args], cwd=cwd or self.root, text=True,
                                capture_output=True, check=False, creationflags=console_flags())
        if result.returncode:
            raise RuntimeError(f"git {' '.join(args)}: {result.stderr.strip()}")
        return result.stdout.strip()

    def path(self, task_id: str, agent: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{12}", task_id) or agent not in {"codex", "claude", "gemini"}:
            raise ValueError("Invalid worktree identity")
        return self.base / task_id / agent

    def ensure(self, task_id: str, agent: str, base: str = "HEAD") -> Path:
        if not re.fullmatch(r"[a-f0-9]{40,64}|HEAD", base):
            raise ValueError("Invalid worktree base")
        path = self.path(task_id, agent)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            branch = f"agent/{agent}/{task_id}"
            self._git("worktree", "add", "-b", branch, str(path), base)
        self.verify(path)
        return path

    def head(self) -> str:
        return self._git("rev-parse", "HEAD")

    def verify(self, path: Path) -> None:
        resolved = path.resolve()
        if not resolved.is_relative_to(self.base.resolve()):
            raise ValueError("Worktree lies outside managed directory")
        top = Path(self._git("rev-parse", "--show-toplevel", cwd=resolved)).resolve()
        if top != resolved:
            raise ValueError("Unexpected Git worktree root")

    def status(self, path: Path) -> str:
        self.verify(path)
        return self._git("status", "--short", cwd=path)

    def tracked_files(self, path: Path) -> list[str]:
        self.verify(path)
        return self._git("ls-files", cwd=path).splitlines()

    def diff(self, path: Path, base: str = "HEAD") -> str:
        self.verify(path)
        if not re.fullmatch(r"[a-f0-9]{40,64}|HEAD", base):
            raise ValueError("Invalid diff base")
        diff = self._git("diff", base, "--", ".", cwd=path)
        untracked = self._git("ls-files", "--others", "--exclude-standard", "-z", cwd=path)
        for name in untracked.split("\0")[:128]:
            if not name:
                continue
            file = path / name
            if not file.is_file() or not file.resolve().is_relative_to(path.resolve()):
                continue
            data = file.read_bytes()[:65536]
            if b"\0" in data:
                addition = f"\nUntracked binary file: {name}\n"
            else:
                content = data.decode("utf-8", errors="replace")
                addition = f"\ndiff --git a/{name} b/{name}\nnew file\n--- /dev/null\n+++ b/{name}\n"
                addition += "".join(f"+{line}\n" for line in content.splitlines())
                if file.stat().st_size > len(data):
                    addition += "+[file truncated]\n"
            diff += addition
        return diff

    def changed_files(self, path: Path, base: str = "HEAD") -> list[str]:
        self.verify(path)
        output = self._git("status", "--porcelain", "--untracked-files=all", cwd=path)
        committed = self._git("diff", "--name-only", base, "--", ".", cwd=path)
        return sorted(set([line[3:] for line in output.splitlines() if len(line) > 3] + committed.splitlines()))

    def cleanup(self, task_id: str, agent: str) -> None:
        path = self.path(task_id, agent)
        if path.exists():
            self.verify(path)
            if self.status(path):
                raise ValueError(f"Worktree {agent} has changes; inspect or save them first")
            self._git("worktree", "remove", str(path))


class CommandPolicy:
    def __init__(self, commands: list[list[str]]):
        self.commands = commands
        for command in commands:
            if not command or not all(isinstance(part, str) for part in command):
                raise ValueError("Test commands must be nonempty argument lists")
            if Path(command[0]).name.lower() in {"git", "git.exe", "cmd", "cmd.exe", "powershell", "pwsh", "bash", "sh"}:
                raise ValueError("Test commands may not invoke a shell or Git")

    def run_tests(self, path: Path, timeout: int = 300) -> list[dict[str, object]]:
        results = []
        for command in self.commands:
            result = subprocess.run(command, cwd=path, text=True, capture_output=True, timeout=timeout, shell=False,
                                    creationflags=console_flags())
            results.append({"command": command, "exit_code": result.returncode,
                            "stdout": result.stdout[-10000:], "stderr": result.stderr[-10000:]})
        return results
