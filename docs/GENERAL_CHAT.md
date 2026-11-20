# General Chat implementation plan

This is the first slice of the Orchestrator roadmap. It adds single-model, text-only Chat Mode to the local Python dashboard.

| Decision | Rationale |
| --- | --- |
| Keep `ChatService` separate from the code coordinator | Chat runs without Git or subprocesses. `ai-team ui --chat-only` also skips Git discovery and CLI quota collection. |
| Add `chat_sessions` and `chat_turns` to the existing SQLite file | Chat can share a dashboard and database with Code Mode while keeping its own lifecycle and storage contract. A turn records the prompt, chosen provider/model, outcome, and usage together. |
| Apply an additive, transactional SQLite migration | Schema version 1 creates the chat tables, leaving the legacy tables intact. Back up `state.sqlite3` with the app stopped or use the SQLite backup API. A rollback to the original code can leave the new tables in place. Restoring a backup also discards anything written after it was taken. |
| Use direct HTTPS calls behind a typed model runner | OpenAI Responses, Anthropic Messages, and Google GenerateContent stay behind one message and usage contract. Direct integrations avoid a gateway account and dependency, while keeping billing and credentials provider-specific. |
| Use guarded `/api/v1` routes and background calls | Persist the pending record before dispatch, cap concurrency, and expire interrupted runs without automatically resubmitting them. The UI polls the session while a turn is pending. |
| Extend the current dashboard with a Chat Mode view | Keep the Python-served interface. Revisit React once the Chat, Compare, and Council API contracts are established. |
| Replay a bounded suffix of successful text exchanges | Keep full local history and show when context was shortened. Ask for consent when included messages cross providers. Attachments, streaming, and tool execution are outside the single-model slice. |
| Read credentials from the server environment | Provider catalog IDs are YAML configuration. Raw provider errors, credentials, and headers are excluded from logs and database writes. Record provider-reported usage and leave cost unknown. |

Verify the baseline and updated suites with `python -m unittest discover -s tests -v` and `PYTHONPATH=src`. Tests should cover legacy migration, session restart, cross-provider history, request deduplication, failures and timeouts, and the local HTTP guards using fake providers. Main integration risks are provider-specific payloads, concurrent submissions, and interrupted runs. Test those at the adapter and session boundaries before adding automated collaboration.

`GET /api/v1/models` returns the configured catalog and key availability. `GET /api/v1/sessions` lists conversations, and `POST /api/v1/sessions` creates a conversation from a provider, model, and optional session settings. `GET /api/v1/sessions/{id}` returns settings and ordered turns. `POST /api/v1/sessions/{id}/messages` accepts a prompt, provider, model, unique request ID, and confirmation to share eligible history across providers when needed. Repeating the same request ID and payload returns the original turn ID. The runner receives typed text messages, instructions, an output cap, and a timeout. It returns response text, model attribution, and optional provider token counts.

Provider contracts are documented in the [OpenAI Responses API](https://developers.openai.com/api/reference/resources/responses/methods/create), [Anthropic Messages API](https://platform.claude.com/docs/en/api/messages/create), and [Google GenerateContent API](https://ai.google.dev/api/generate-content).

## Compare and AI Council

Schema version 2 adds strategy and participant settings to chat sessions and stores each multi-model run as bounded JSON in `chat_runs`. Existing sessions default to `single` and preserve their messages. Use a pre-upgrade SQLite backup to roll back to the version 1 Chat implementation. Version 2 retains Code Mode's existing tables.

Create a session with `strategy` set to `compare` or `council` and pass two or three `{provider, model}` participants. `synthesizer` is a zero-based participant index. Optional settings include `review_rounds` (zero to three), `auto_advance`, `max_invocations`, and `token_budget`. Submit a prompt through the existing session messages endpoint with optional `context` and, for a Council spanning providers, `share_history: true`. Session details return saved `runs`, each with stages, replies, usage, and applicable controls. Each reply stores the IDs of any upstream replies it was given and indicates if excerpts were shortened. New prompts start independent runs using the supplied context.

The authenticated `POST /api/v1/sessions/{id}/runs/{run_id}/{action}` endpoint accepts `pause`, `stop`, `next`, `retry`, `synthesize`, `choose`, `expand`, and `promote`. Use the run's current `revision` for dispatch actions and `reply_id` for retry, selection, or follow-up. `expand` also takes `instruction`. `promote` takes a unique `request_id` and copies independent answers into a new Council without dispatching a provider call. Compare synthesis and promotion ask for a sharing confirmation. The synthesis response is checked for the five output fields. A malformed response remains visible and can be retried.

Each stage freezes its input reply set before dispatch. A worker records each attempt before invoking the provider. Pause and stop take effect at the next dispatch boundary. Process exit and expired leases mark open attempts interrupted, and a resumed worker uses a new claim so a late reply cannot overwrite newer state.

## Collaboration presets and reusable teams

The session API also accepts `roundtable`, `relay`, and `debate` as strategy values. These presets accept up to four distinct participants. Each participant may have a `role` and `instructions`, which the runner adds to that participant's system prompt. Mixed-provider preset runs require `share_history: true` on submission.

- **Roundtable** schedules one speaker per stage with a bounded `roundtable_turns` count from 1 to 16. Each speaker receives earlier successful turns from the same run. The initial message and the `next` action can take an optional zero-based `participant` index. The `next` action can also take an `instruction` when the previous stage is done. When no index is given, a leading provider alias or model ID prefixed with `@` can select a speaker. A provider alias that matches several models requires disambiguation. Otherwise the scheduler uses participant order.
- **Relay** schedules one stage per participant and passes the last successful handoff together with the original prompt and shared context. Automatic advancement pauses after a failure. Manual advancement can skip the failed step and identifies missing work to the next model.
- **Debate** requires at least three participants. The zero-based `judge` index defaults to the final participant. Opening arguments are independent. Each of the one to three `rebuttal_rounds` receives a fixed set of the earlier arguments and rebuttals. The judge receives the available record and uses the same five-field response schema as Council synthesis. A malformed assessment can be retried.

Schema version 4 adds `chat_teams` alongside existing conversations and runs. The authenticated `GET /api/v1/teams` lists saved teams, and `POST /api/v1/teams` accepts a team name and the fields used to create a multi-model session. Each response contains the team's strategy, `strategy_options`, and conversation settings. The React dashboard copies these into a new-conversation form. Saved teams contain configuration and prompts, with no conversation history or provider credentials. The classic Chat dashboard links to React for the three new presets.

## Live interaction

Schema version 3 adds the cancellation timestamp to single-model turns. Cancelled requests are terminal. The database records the message, status, and completion time while live preview text stays in memory. The version 3 migration keeps both prior session types and Code Mode tables readable.

`GET /api/v1/sessions/{id}/events` returns `sequence`, `state_sequence`, and the current `drafts` by attempt key. Supply `?after={sequence}` to hold the request for up to eight seconds or until a change. The client rereads the durable session when `state_sequence` changes, and refreshes active sessions on a heartbeat to recover from server changes or expired jobs. Each response is a replaceable snapshot, so reconnecting does not replay messages. The same local token, Host, and Origin guards apply. Each server limits simultaneous listeners to 16 and keeps at most 256 recent session cursors.

`POST /api/v1/sessions/{id}/cancel` takes a `turn_id`. The group equivalent is `POST /api/v1/sessions/{id}/runs/{run_id}/cancel`. Cancellation records the terminal state before interrupting local provider sockets. An operation still establishing a connection can take until its network timeout to return. The provider can continue processing or charge for a request it already received. Late results cannot overwrite cancelled state. Code Mode retains its existing CLI process controls.

## Branching, export, and Code handoff

Schema version 5 adds a source checkpoint to branches and a link table for approved chat-to-Code handoffs. Existing conversations and tasks remain usable.

- `POST /api/v1/sessions/{id}/branch` accepts a saved turn ID or run ID as `checkpoint` and an idempotent `request_id`. The branch copies the prefix through that point in one transaction. Active turns and runs cannot be copied. Copied runs retain their replies and attribution as read-only history.
- `GET /api/v1/sessions/{id}/export?format=markdown|json&diagnostics=0|1` returns an attachment. The default is Markdown without diagnostics. Export reads the durable conversation and linked task titles. Current provider credential values are redacted. Code diffs, test logs, and streaming drafts are outside the export.
- In a Code-enabled dashboard, `GET /api/v1/sessions/{id}/handoff` prepares an editable draft locally from recent saved prompts, replies, and any structured synthesis. It makes no provider calls. `POST` on the same route requires the reviewed `brief`, current `repository`, chosen `roles` and `recipient`, an idempotent `request_id`, and `confirmed: true`. Task creation and the conversation link commit together before the Code scheduler runs.
- `GET /api/v1/sessions/{id}/artifacts/{task_id}` requires the task to be linked to that conversation. It returns the approved brief, recent agent updates, recorded diffs and changed files, and test summaries. The snapshots come from existing Code Mode records and remain available if a task's worktree is removed. The response excludes raw test output and raw CLI output.

These routes use the same local token and Host checks as the session API. Mutations also check Origin. The handoff target is the repository of the running dashboard.

OpenAI and Anthropic use streaming responses on their existing endpoints. Gemini uses `streamGenerateContent` with SSE. Adapters render visible text deltas, ignore thinking events, and require a provider completion signal before committing a reply. Provider-specific API details are documented in the [OpenAI Responses streaming events](https://developers.openai.com/api/reference/resources/responses/streaming-events/), [Anthropic streaming Messages](https://platform.claude.com/docs/en/build-with-claude/streaming), and [Gemini GenerateContent API](https://ai.google.dev/api/generate-content#method:-models.streamgeneratecontent).

Compare and Council sessions default to parallel independent replies. Set `parallel: false` to run all stages sequentially. Independent answers can share the four-slot server capacity, subject to the run's invocation cap. Stages that use peer replies wait for the preceding stage. When a token budget is configured, calls run sequentially so the scheduler can check provider-reported usage before starting the next one.
