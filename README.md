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

`init` creates `.ai-team/config.yaml` and editable context files. Edit the command paths and flags there to match your installed CLI versions. `start --role codex="..."` overrides an agent's role for one task. Use `ai-team resume <task-id>` for a persisted queued task; `stop` halts it; `cleanup` removes only clean worktrees and preserves task history. `--project PATH` targets another Git repository.

The initial conversation runs Codex, then Claude, then Gemini. Subsequent requests use structured `delegate_to`/`delegated_task` fields or a leading `@agent` message line. Max turns, rounds, repeated requests, and empty delegation tasks are guarded. A blocked response or exhausted queue requiring a decision moves the task to `awaiting_human`. There is no automatic merge command.

If a CLI process fails (for example, because a network proxy is unavailable), its turn stays queued and the task becomes `failed`. Run `ai-team retry <task-id>` after fixing the cause. `resume` is for tasks that are still running with queued turns.

Claude Code 2.1.283 and Antigravity CLI 1.2.11 were found locally by absolute path, though this app session's PATH does not include their install directories. The end-to-end flow remains verified with simulated adapters. If you initialized this project before the Antigravity update, change the `gemini` entry in `.ai-team/config.yaml` to launch `agy`, remove `--approval-mode plan`, and set `format: antigravity-json`; the default now passes a response schema through `--json-schema` and selects the installed CLI's `--mode plan` for review. The default does not bypass permissions. Worktree change detection is an additional check, not a filesystem guarantee. See [the design and risk notes](docs/PLAN.md).

## Development

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```
