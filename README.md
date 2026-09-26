# AI Team

A local Python CLI that coordinates Codex, Claude Code, and Gemini CLI across separate Git worktrees. It persists conversations in SQLite, passes findings between agents, supports bounded delegation, and leaves all merges to the human.

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

Claude Code and Gemini CLI were unavailable on the development machine, so the end-to-end flow was verified with simulated adapters. Check their flags and permission modes on your installations before a live run. See [the design and risk notes](docs/PLAN.md).

## Development

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```
