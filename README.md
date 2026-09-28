# AI Team

A local Python CLI that coordinates Codex, Claude Code, and Google Antigravity CLI across separate Git worktrees. It persists conversations in SQLite, passes findings between agents, supports bounded delegation, and leaves all merges to the human. The Google agent retains the internal name `gemini`, including `@gemini` delegation, while launching Antigravity's `agy` command.

## Quick start

Requires Python 3.11+, Git, and the three agent CLIs installed and authenticated.

```powershell
python -m pip install -e .
ai-team init
ai-team start "Inspect the parser for data loss"
ai-team status
ai-team chat <task-id>
ai-team diff <task-id>
ai-team review <task-id>
```

## Browser interface

### One-click launch (Windows)

Run this once per project to put an **AI Team - <project>** shortcut on your desktop:

```powershell
$env:PYTHONPATH = "src"   # skip if installed with pip
python -m ai_team --project C:\path\to\repo shortcut
```

Double-click the shortcut to open the dashboard with no console window. Right-click it to pin it to Start or the taskbar. The shortcut works without a pip install. Double-clicking it again while the dashboard is running reopens the existing dashboard instead of starting a second server. Use **Quit** in the dashboard's top bar to stop it. Startup errors appear in a dialog.

### From a terminal

Run `ai-team ui` anywhere inside a repository; without an editable install, use `$env:PYTHONPATH = "src"` followed by `python -m ai_team ui`. The first launch in a repository runs `init` automatically. The server binds only to `127.0.0.1`, starting at port 8765. Each project keeps a stable port, and relaunching reopens the running dashboard instead of starting another. If another program or project holds the port, the next free port up to 8774 is used. Use `--port 0` to choose any available port or `--no-browser` to print the address without opening a tab. Press Ctrl+C or use **Quit** to stop the interface. Stopping interrupts any agent turn in progress.

The dashboard opens on the newest task that needs you (running, waiting for direction, or failed). Otherwise it opens a blank task with the message box focused, so you can type immediately. It shows tasks, conversations, changed files, diffs, and test results; it can start, resume, retry, and stop tasks. Use the **+** button to open a blank task and select an implementor, Q&A, reviewer, or QA role for each agent. The implementor runs first; other roles are read-only. The dashboard shows which agent is active during a turn. The message box at the bottom sends to the open task; start a message with `@Codex`, `@Claude`, `@Gemini` (Antigravity), or `@Everyone` to choose recipients, and press Ctrl+Enter to send. A task addressed to one agent needs only that agent's CLI installed. Wait for an active turn to finish before sending another message. Stop takes effect after the current agent call. The agent sidebar shows tokens from each agent's latest recorded run and remaining plan quota. The server reads Codex account limits, Claude `/usage`, and Antigravity `/usage` on startup and after each agent turn. If a provider cannot return a quota, the last successful snapshot remains visible with its check time, or the sidebar says unavailable.

### Finding the agent CLIs

Agent `command` values can stay as bare names (`codex`, `claude`, `agy`). The coordinator looks on PATH first and then in each installer's standard location: `%LOCALAPPDATA%\OpenAI\Codex\bin\<version>\codex.exe` (newest version), `~\.local\bin\claude.exe`, and `%LOCALAPPDATA%\agy\bin\agy.exe`. A configured absolute path that no longer exists, such as a Codex path from before an update, falls back to the same search.

`init` creates `.ai-team/config.yaml` and editable context files. Edit the command paths and flags there to match your installed CLI versions. `start --role claude=implementation --role 'codex=q&a'` assigns roles for one task. Valid roles are `implementation`, `q&a`, `review`, and `qa`; Q&A and QA have the same read-only permissions, with different role labels in the agent prompt. Use `ai-team resume <task-id>` for a persisted queued task; `stop` halts it; `cleanup` removes only clean worktrees and preserves task history. `--project PATH` targets another Git repository.

By default, the initial conversation runs Codex, then Claude, then Gemini. A selected implementor runs first. Subsequent requests use structured `delegate_to`/`delegated_task` fields or a leading `@agent` message line. Max turns, rounds, repeated requests, and empty delegation tasks are guarded. A blocked response or exhausted queue requiring a decision moves the task to `awaiting_human`. There is no automatic merge command.

If a CLI process fails (for example, because a network proxy is unavailable), its turn stays queued and the task becomes `failed`. Run `ai-team retry <task-id>` after fixing the cause. `resume` is for tasks that are still running with queued turns.

If you initialized this project before the Antigravity update, change the `gemini` entry in `.ai-team/config.yaml` to launch `agy`, remove `--approval-mode plan`, and set `format: antigravity-json`; the default passes a response schema through `--json-schema` and selects `--mode plan` for review. For read-only QA, the orchestrator lists tracked files and asks Antigravity to inspect them with `view_file`, without shell commands or broader permissions. Tasks that genuinely require shell commands need scoped Antigravity allow rules. Worktree change detection is an additional check, not a filesystem guarantee. See [the design and risk notes](docs/PLAN.md).

## Development

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```
