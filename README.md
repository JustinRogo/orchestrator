# AI Team

A local Python CLI that coordinates Codex, Claude Code, and Google Antigravity CLI across separate Git worktrees. It persists conversations in SQLite, passes findings between agents, supports bounded delegation, and leaves all merges to the human. The Google agent retains the internal name `gemini`, including `@gemini` delegation, while launching Antigravity's `agy` command.

The browser dashboard also has a Chat Mode for text conversations with OpenAI, Anthropic, and Google API models.

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

### General Chat Mode

Set the environment variables for the providers you want to use before launching the dashboard. Use `OPENAI_API_KEY` for OpenAI, `ANTHROPIC_API_KEY` for Anthropic, and `GEMINI_API_KEY` for Google. API keys and billing are separate from consumer-chat subscriptions and coding CLI accounts. API calls may incur charges. Set keys in the terminal or system credential environment you use to launch the app. Keep them out of the YAML file and the conversation.

Run `ai-team ui` from your repository and open **Chat Mode**, or launch chat in any existing directory without Git or CLI agents:

```powershell
ai-team --project C:\path\to\chat-workspace ui --chat-only
```

Without a package install, set `PYTHONPATH` to the absolute path of this checkout's `src` directory and use `python -m ai_team` in place of `ai-team`.

Start a conversation and choose a model. Open **Conversation settings** before starting to set system instructions, a turn limit, maximum output tokens per turn, and a timeout. Use **Model for next reply** to switch models in an existing conversation. The selected provider receives the new message, system instructions, and up to 12 recent successful exchanges within a 40,000-byte history limit. The dashboard asks before including exchanges from another provider and marks responses when older exchanges were omitted. The full conversation stays available in **History** after a restart.

The model picker uses a configurable catalog. To override it, edit or create `.ai-team/config.yaml` in the workspace and reload the page. Omitted providers keep their defaults and an empty list removes a provider's models. Use text-generation model IDs your API account can access. A configured key in the picker indicates that the environment variable is set. The provider verifies access when you send a message.

```yaml
chat:
  models:
    openai: [gpt-6.1-terra]
    anthropic: [claude-sonnet-4-6]
    google: [gemini-3.5-flash-lite]
```

Chat Mode streams text directly from the selected API and reports provider token totals when available. **Cancel reply** closes the local request once its connection is established. The provider may still finish or charge for work it received. Each submission counts toward the session's turn limit, including failures and cancellations. The dashboard saves the outcome and leaves draft text out of conversation history. A clean exit marks unfinished turns interrupted immediately. After a crash, a pending turn expires at its configured timeout. Chat data lives in `.ai-team/state.sqlite3` alongside any Code Mode history. See the [implementation and migration plan](docs/GENERAL_CHAT.md).

### Compare and AI Council

In **Chat Mode**, choose **New conversation**, then select **Compare** or **AI Council**. Select two or three distinct models and choose the synthesis model. Each new run starts with the prompt and any shared context you supply for that run.

- **Compare** shows independent answers side by side. Choose a reply to mark it, ask its model a private follow-up, synthesize the available responses, or send the independent answers to a new Council without repeating those API calls.
- **AI Council** starts with the same independent answers, then sends labeled replies into peer review. The selected model synthesizes a recommendation, agreement, disagreement, uncertainties, and next steps. Use **Next stage** to advance manually, enable automatic advancement, or choose **Synthesize now** once an answer is available. Council asks for consent before sharing replies across providers. Prior answers and reviews stay visible alongside the synthesis.
- **Pause** waits for active requests and leaves the run available to continue. **Stop** waits for active requests and ends the run. **Cancel run** ends the run, interrupts its active local streams, and keeps replies already saved. A failed model's reply can be retried in the current stage. You can also advance with the results you have.

**Conversation settings** control the per-run call limit, optional token budget, number of peer review rounds, and parallel independent replies. Compare and the Council's opening stage can stream several independent answers at once. A configured token budget makes calls run one at a time, so the scheduler can check reported usage before each new call. A single call can exceed the budget before usage is reported. Calls also run sequentially when parallel replies are disabled. Dollar costs are unavailable. Compare and Council runs reopen from **History** with replies, errors, usage, and the reply IDs used as context.

### Collaboration presets and saved teams

Choose one of these modes under **New conversation** in the React dashboard.

- **Roundtable** gives two to four models a shared discussion. Start a prompt with an @mention such as `@GPT`, `@Claude`, `@Gemini`, or `@<model-id>`, or use **Opening speaker**. Use **Next turn** to follow the discussion order or **Direct next turn** to choose a speaker and add a question or instruction. The default run gives the team one turn per model.
- **Collaborative Relay** passes work through two to four models in order. The default roles are Draft, Critique, Validate, and Revise. Customize each role and its instructions. If a step fails, retry it or continue with the last successful handoff.
- **Structured Debate** uses two or three debaters and a separate judge. Set the viewpoints and judge, then run independent opening arguments, rebuttals, and a structured assessment of the evidence and remaining disagreements. One rebuttal round is planned by default.

Advance manually or enable automatic advancement in **Conversation settings**. Runs use the same sharing confirmation for mixed providers, call limits, token budget, and pause, stop, and cancel controls as AI Council. Each new run starts with its own prompt and shared context.

Open **Save as a reusable team** on the new-conversation form to store a named setup with its model lineup, roles, participant instructions, system prompt, and run settings. **Use a saved team** fills a new form that you can edit before starting. Teams are stored in the local workspace database.

### Branch, export, and hand off a conversation

Open **Conversation tools** above an existing conversation in the React dashboard.

- **Branch conversation** copies the history through a saved turn or run and opens a new conversation with its own prompt limit. Single Chat uses eligible copied replies as context. Group branches keep the copied runs as history and use the shared prompt and context you provide for new runs.
- **Export conversation** downloads Markdown or JSON. Usage, error, and truncation details are optional. Review the download before sharing it because it includes the saved conversation text.
- **Turn into Coding Task** prepares an editable brief from the saved discussion, including Council recommendations and dissent when available. Verify the current repository, edit the brief and agent roles, then confirm and start. The implementor works in an isolated Git worktree. A handoff cannot merge changes.

The source conversation links to the Code task and its saved agent updates, diffs, and test summaries. Use **View artifacts** in **Conversation tools** to inspect them. Code Mode links back to the source conversation. To start a handoff, open Chat from the full dashboard with `ai-team ui` in the target repository.

### One-click launch (Windows)

Run this once per project to put an **AI Team - <project>** shortcut on your desktop:

```powershell
$env:PYTHONPATH = "src"   # skip if installed with pip
python -m ai_team --project C:\path\to\repo shortcut
```

Double-click the shortcut to open the dashboard with no console window. Right-click it to pin it to Start or the taskbar. The shortcut works without a pip install. Double-clicking it again while the dashboard is running reopens the existing dashboard instead of starting a second server. Use **Quit** in the dashboard's top bar to stop it. Startup errors appear in a dialog.

### From a terminal

Run `ai-team ui` anywhere inside a repository; without an editable install, use `$env:PYTHONPATH = "src"` followed by `python -m ai_team ui`. The first launch in a repository runs `init` automatically. The server binds only to `127.0.0.1`, starting at port 8765. Each project keeps a stable port, and relaunching reopens the running dashboard instead of starting another. If another program or project holds the port, the next free port up to 8774 is used. Use `--port 0` to choose any available port or `--no-browser` to print the address without opening a tab. Press Ctrl+C or use **Quit** to stop the interface. Stopping interrupts any agent turn in progress.

The dashboard opens on the newest task that needs you (running, waiting for direction, or failed). Otherwise it opens a blank task with the message box focused, so you can type immediately. It shows tasks, conversations, changed files, diffs, and test results; it can start, resume, retry, and stop tasks. Choose **New task** to open a blank task and select an implementor, Q&A, reviewer, or QA role for each agent. The implementor runs first; other roles are read-only. The dashboard shows which agent is active during a turn. The message box at the bottom sends to the open task; start a message with `@Codex`, `@Claude`, `@Gemini` (Antigravity), or `@Everyone` to choose recipients, and press Ctrl+Enter to send. A task addressed to one agent needs only that agent's CLI installed. Wait for an active turn to finish before sending another message. Stop takes effect after the current agent call. The agent sidebar shows tokens from each agent's latest recorded run and remaining plan quota. The server reads Codex account limits, Claude `/usage`, and Antigravity `/usage` on startup and after each agent turn. If a provider cannot return a quota, the last successful snapshot remains visible with its check time, or the sidebar says unavailable.

### Finding the agent CLIs

Agent `command` values can stay as bare names (`codex`, `claude`, `agy`). The coordinator looks on PATH first and then in each installer's standard location: `%LOCALAPPDATA%\OpenAI\Codex\bin\<version>\codex.exe` (newest version), `~\.local\bin\claude.exe`, and `%LOCALAPPDATA%\agy\bin\agy.exe`. A configured absolute path that no longer exists, such as a Codex path from before an update, falls back to the same search.

`init` creates `.ai-team/config.yaml` and editable context files. Edit the command paths and flags there to match your installed CLI versions. `start --role claude=implementation --role 'codex=q&a'` assigns roles for one task. Valid roles are `implementation`, `q&a`, `review`, and `qa`; Q&A and QA have the same read-only permissions, with different role labels in the agent prompt. Use `ai-team resume <task-id>` for a persisted queued task; `stop` halts it; `cleanup` removes only clean worktrees and preserves task history. `--project PATH` targets another Git repository.

By default, the initial conversation runs Codex, then Claude, then Gemini. A selected implementor runs first. Subsequent requests use structured `delegate_to`/`delegated_task` fields or a leading `@agent` message line. Max turns, rounds, repeated requests, and empty delegation tasks are guarded. A blocked response or exhausted queue requiring a decision moves the task to `awaiting_human`. There is no automatic merge command.

If a CLI process fails (for example, because a network proxy is unavailable), its turn stays queued and the task becomes `failed`. Run `ai-team retry <task-id>` after fixing the cause. `resume` is for tasks that are still running with queued turns.

If you initialized this project before the Antigravity update, change the `gemini` entry in `.ai-team/config.yaml` to launch `agy`, remove `--approval-mode plan`, and set `format: antigravity-json`; the default passes a response schema through `--json-schema` and selects `--mode plan` for review. For read-only QA, the orchestrator lists tracked files and asks Antigravity to inspect them with `view_file`, without shell commands or broader permissions. Tasks that genuinely require shell commands need scoped Antigravity allow rules. Worktree change detection is an additional check, not a filesystem guarantee. See [the design and risk notes](docs/PLAN.md).

## Development

### React dashboard

The React dashboard supports Code Mode, single-model Chat, Compare, and AI Council. It uses the same local Python APIs and SQLite history. Code Mode includes task and agent controls, usage and quota snapshots, and an inspector for worktree diffs and test results.

Build the frontend with Node.js 22.12 or newer from the repository root:

```powershell
npm --prefix frontend ci
npm --prefix frontend run build
```

Launch or restart `ai-team ui` after building. The dashboard opens in **Code Mode** at `/`. **Chat Mode** opens at `/chat`, and `ai-team ui --chat-only` opens directly into Chat. The previous `/chat/preview` address still opens Chat. The classic views are available at `/classic` and `/classic/chat`. A Python-only installation opens the classic dashboard by default.

For frontend changes, use `npm --prefix frontend run watch` and refresh the dashboard in the browser afterwards.

Run the frontend build before packaging a Python wheel to include the preview assets. The classic dashboard works from a Python installation on its own.

### Python tests

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```
