# MVP design and implementation plan

## Environment and CLI integration

Python 3.11.3, Git, PyYAML 6.0, and Codex CLI 0.158.0-alpha.2.1 were found locally. Claude Code and Gemini CLI were not on PATH during development. Codex `exec --json -` accepts a prompt on stdin and emits JSONL. Claude Code's documented print mode supports `-p --output-format json`; Gemini's documented headless mode supports `-p <prompt> --output-format json`. Their commands and flags are settings, so installations can adapt to version differences.

Uncertainties to verify on the user's actual installs: exact JSON envelope, whether the installed versions support the configured plan modes, authentication/environment needs, and whether headless mode exits promptly on approval requests. The parser tolerates plain text and common JSON envelopes, while logs retain raw output for diagnosis.

## Structure and flow

`src/ai_team/config.py` loads `.ai-team/config.yaml`; `store.py` persists tasks, messages, and invocations in SQLite; `git.py` manages worktrees and allowed test commands; `adapters.py` invokes CLIs; `coordinator.py` builds bounded context and runs the conversation; `cli.py` exposes commands. `.ai-team/` also stores project context, decisions, artifacts, and JSONL logs.

For each task, enabled agents receive separate worktrees under `.ai-team/worktrees/<task-id>/<agent>` on `agent/<agent>/<task-id>` branches. Codex runs first, then Claude, then Gemini. Each receives recent messages. Review agents also receive the primary worktree's current diff. The coordinator accepts structured `delegate_to` and `delegated_task` first, then leading `@agent` or `@all` lines. It rejects empty, repeated, disabled, or self delegations, and bounds turns and rounds. All responses and CLI details are persisted. No merge path exists.

## Risks and limits

Git worktrees isolate working directories, not operating-system access. CLI agents may still be able to access files outside the project. The runner passes only an environment allowlist, but agent tools may load their own settings and secrets. Claude and Gemini default to documented plan modes for read-only review; these flags have not been checked against local installs. The coordinator also detects worktree changes, but this is not an operating-system sandbox. The test policy accepts only configured executable/argument arrays and rejects shell and Git launchers; it does not execute agent-suggested commands. Worktree cleanup refuses dirty trees. Outputs and prompts can contain sensitive source material, so protect `.ai-team/logs` and the SQLite database.

## Milestones

1. Implement and test the fixed Codex → Claude → Gemini conversation and persistence.
2. Add bounded structured and mention-based delegation, review and inspection commands, and worktree lifecycle.
3. Verify live CLI integration once Claude and Gemini are installed; refine permission flags and JSON parsers for their installed versions.
4. Later: human decision workflow, merge approval, web interface, streaming, parallelism, cost budgets, and stronger process isolation.
