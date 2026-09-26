# MVP design and implementation plan

## Environment and CLI integration

Python 3.11.3, Git, PyYAML 6.0, and Codex CLI 0.158.0-alpha.2.1 were found locally. Claude Code 2.1.283 and Antigravity CLI 1.2.11 were later found by absolute path, though their install directories are not on this app session's PATH. Codex `exec --json -` accepts a prompt on stdin and emits JSONL. Claude Code's print mode supports `-p --output-format json`; Antigravity's headless mode supports `agy -p <prompt> --output-format json --json-schema <schema>`. The installed Antigravity help also supports `--mode plan`. Commands and flags are settings, so installations can adapt to version differences. The Google agent keeps its internal `gemini` key for existing messages and configuration, but uses an Antigravity-specific parser for its `status`, `response`, `structured_output`, and `usage` envelope.

Uncertainties to verify with live calls: Claude's exact JSON envelope, authentication/environment needs, and whether headless mode exits promptly on approval requests. The parser tolerates plain text and common JSON envelopes, while logs retain raw output for diagnosis. Antigravity headless runs require cached authentication; its documented JSON envelope is covered by a parser test.

## Structure and flow

`src/ai_team/config.py` loads `.ai-team/config.yaml`; `store.py` persists tasks, messages, and invocations in SQLite; `git.py` manages worktrees and allowed test commands; `adapters.py` invokes CLIs; `coordinator.py` builds bounded context and runs the conversation; `cli.py` exposes commands. `.ai-team/` also stores project context, decisions, artifacts, and JSONL logs.

For each task, enabled agents receive separate worktrees under `.ai-team/worktrees/<task-id>/<agent>` on `agent/<agent>/<task-id>` branches. Codex runs first, then Claude, then Gemini. Each receives recent messages. Review agents also receive the primary worktree's current diff. The coordinator accepts structured `delegate_to` and `delegated_task` first, then leading `@agent` or `@all` lines. It rejects empty, repeated, disabled, or self delegations, and bounds turns and rounds. All responses and CLI details are persisted. No merge path exists.

## Risks and limits

Git worktrees isolate working directories, not operating-system access. CLI agents may still be able to access files outside the project. The runner passes only an environment allowlist, but agent tools may load their own settings and secrets. Claude and Antigravity default to their installed CLIs' plan modes for review. The coordinator also detects worktree changes, but this is not an operating-system sandbox. Antigravity permission rules may be configured separately in its global settings. The test policy accepts only configured executable/argument arrays and rejects shell and Git launchers; it does not execute agent-suggested commands. Worktree cleanup refuses dirty trees. Outputs and prompts can contain sensitive source material, so protect `.ai-team/logs` and the SQLite database.

## Milestones

1. Implement and test the fixed Codex → Claude → Gemini conversation and persistence.
2. Add bounded structured and mention-based delegation, review and inspection commands, and worktree lifecycle.
3. Verify live CLI invocation and authentication; refine permission settings and JSON parsers if the installed versions differ from documented outputs.
4. Later: human decision workflow, merge approval, web interface, streaming, parallelism, cost budgets, and stronger process isolation.
