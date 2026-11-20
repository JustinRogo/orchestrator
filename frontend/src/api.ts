import type { AgentMap, CodeOverview, Recipient, Role, TaskAction, TaskChanges, TaskView } from './codeTypes';

export type Strategy = 'single' | 'compare' | 'council' | 'roundtable' | 'relay' | 'debate';
export type GroupStrategy = Exclude<Strategy, 'single'>;

export interface ModelChoice {
  provider: string;
  model: string;
}

export interface Participant extends ModelChoice {
  role?: string;
  instructions?: string;
}

export interface Provider {
  id: string;
  name: string;
  configured: boolean;
  credential_variable: string;
  models: string[];
}

export interface ConversationLimits {
  max_turns: number;
  max_output_tokens: number;
  timeout_seconds: number;
}

export interface ConversationSettings extends ConversationLimits {
  system_prompt: string;
}

export interface ModelCatalog {
  providers: Provider[];
  defaults: ConversationLimits;
}

export interface SessionSummary extends ModelChoice {
  id: string;
  title: string;
  strategy: Strategy;
}

export interface GroupSettings {
  participants: Participant[];
  synthesizer: number;
  review_rounds: number;
  auto_advance: boolean;
  max_invocations: number;
  token_budget: number | null;
  parallel?: boolean;
  roundtable_turns?: number;
  judge?: number;
  rebuttal_rounds?: number;
}

export type Checkpoint = number | string;
export type Session = SessionSummary & ConversationSettings & {
  branch_source?: { session_id: string; title: string; checkpoint: Checkpoint; history_count: number } | null;
};
export interface CodeLink {
  task_id: string;
  title: string;
  created_at: string;
}
export type Brief = Record<'objective' | 'requirements' | 'decisions' | 'rejected_alternatives' |
  'uncertainties' | 'acceptance_criteria' | 'context', string>;
export interface HandoffDetails {
  brief: Brief;
  repository: string;
  roles: AgentMap<Role>;
  recipient: Recipient;
}
export interface HandoffDraft extends HandoffDetails {
  agents: CodeOverview['agents'];
  role_options: CodeOverview['role_options'];
}
export interface LinkedOutputs {
  status: string;
  handoff: HandoffDetails;
  created_at: string;
  updates: { id: string; agent: string; content: string; timestamp: string; truncated: boolean }[];
  changes: TaskChanges;
  tests: { agent: string; command: string[] | null; exit_code: number | null; error: string | null }[];
}
export type ExportFormat = 'markdown' | 'json';
export type GroupSession = Session & { strategy: GroupStrategy; strategy_options: GroupSettings };
export type NewSession = ConversationSettings & (
  { strategy: 'single' } & ModelChoice | { strategy: GroupStrategy } & GroupSettings
);
export type NewGroupSession = Extract<NewSession, { strategy: GroupStrategy }>;
export type SavedTeam = ConversationSettings & {
  id: string;
  name: string;
  strategy: GroupStrategy;
  strategy_options: GroupSettings;
};

export interface ChatTurn extends ModelChoice {
  id: number;
  status: 'pending' | 'complete' | 'failed' | 'interrupted' | 'cancelled';
  prompt: string;
  response: string | null;
  response_model: string | null;
  error: string | null;
  total_tokens: number | null;
  context_truncated: 0 | 1;
  output_limited: 0 | 1 | null;
}

export interface ChatView {
  session: Session;
  handoffs: CodeLink[];
  turns: ChatTurn[];
  context_providers: string[];
}

export type RunStatus = 'ready' | 'running' | 'pausing' | 'stopping' | 'paused' | 'complete'
  | 'stopped' | 'limited' | 'interrupted' | 'cancelled';

export interface Synthesis {
  agreement: string[];
  disagreement: string[];
  uncertainties: string[];
  recommendation: string;
  next_steps: string[];
}

export interface GroupReply extends ModelChoice {
  id: number;
  participant: number;
  status: ChatTurn['status'];
  input_reply_ids: number[];
  context_truncated: boolean;
  output_limited?: boolean;
  response?: string | null;
  response_model?: string | null;
  error?: string | null;
  total_tokens?: number | null;
  synthesis?: Synthesis | null;
  finished_at?: string;
  imported?: boolean;
}

export interface RunStage {
  id: number;
  kind: 'answer' | 'review' | 'synthesis' | 'expand' | 'roundtable' | 'relay' | 'argument' | 'rebuttal' | 'judgment';
  done: boolean;
  instruction: string;
  targets: number[];
  replies: GroupReply[];
}

export interface GroupRun {
  id: string;
  revision: number;
  prompt: string;
  context: string;
  status: RunStatus;
  incomplete: boolean;
  inherited?: boolean;
  note: string | null;
  stages: RunStage[];
  selected_reply: number | null;
  source: { session_id: string; run_id: string } | null;
  budget: { calls: number; max_calls: number; used: { tokens: number | null }; limit: { tokens: number | null } };
  controls: {
    pause: boolean;
    stop: boolean;
    cancel: boolean;
    next: boolean;
    synthesize: boolean;
    retry: number[];
    expand: boolean;
    promote: boolean;
  };
}

export interface GroupView {
  session: GroupSession;
  handoffs: CodeLink[];
  runs: GroupRun[];
}

export type SessionView = ChatView | GroupView;

export type RunAction = { kind: 'pause' | 'stop' | 'cancel' }
  | { kind: 'next'; auto_advance?: boolean; participant?: number; instruction?: string }
  | { kind: 'choose' | 'retry'; reply_id: number }
  | { kind: 'expand'; reply_id: number; instruction: string }
  | { kind: 'synthesize'; share_history?: boolean }
  | { kind: 'promote'; share_history: true; request_id: string };

export const isRunActive = (run: GroupRun) => ['running', 'pausing', 'stopping'].includes(run.status);
export const newRequestId = () => crypto.randomUUID().replaceAll('-', '');

export interface LiveSnapshot {
  sequence: number;
  state_sequence: number;
  drafts: Record<string, string>;
}

export interface MessageSubmission extends ModelChoice {
  prompt: string;
  request_id: string;
  share_history: boolean;
}

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

export function createApi(token: string) {
  async function fetchResponse(path: string, signal?: AbortSignal, body?: object): Promise<Response> {
    const headers: Record<string, string> = { 'X-AI-Team-Token': token };
    if (body) headers['Content-Type'] = 'application/json';
    let response: Response;
    try {
      response = await fetch(path, {
        method: body ? 'POST' : 'GET',
        headers,
        cache: 'no-store',
        signal,
        body: body ? JSON.stringify(body) : undefined,
      });
    } catch (error) {
      if (signal?.aborted) throw error;
      throw new Error('Could not reach AI Team. Check that the local server is running.');
    }
    if (response.status === 403) {
      throw new ApiError('This page lost its connection. Reload it to reconnect to AI Team.', 403);
    }
    if (!response.ok) {
      const payload = await response.json().catch(() => null);
      throw new ApiError(payload?.error || 'AI Team returned an invalid response.', response.status);
    }
    return response;
  }

  async function request<T>(path: string, signal?: AbortSignal, body?: object): Promise<T> {
    const response = await fetchResponse(path, signal, body);
    const payload = await response.json().catch(() => null);
    if (payload === null) throw new ApiError('AI Team returned an invalid response.', response.status);
    return payload as T;
  }

  const sessions = '/api/v1/sessions';
  const sessionPath = (id: string) => `${sessions}/${encodeURIComponent(id)}`;
  const tasks = '/api/tasks';
  const taskPath = (id: string) => `${tasks}/${encodeURIComponent(id)}`;

  return {
    models: (signal?: AbortSignal) => request<ModelCatalog>('/api/v1/models', signal),
    listSessions: (signal?: AbortSignal) => request<{ sessions: SessionSummary[] }>(sessions, signal),
    createSession: (body: NewSession) => request<{ session: Session }>(sessions, undefined, body),
    listTeams: (signal?: AbortSignal) => request<{ teams: SavedTeam[] }>('/api/v1/teams', signal),
    saveTeam: (body: NewGroupSession & { name: string }) => request<{ team: SavedTeam }>('/api/v1/teams', undefined, body),
    openSession: (id: string, signal?: AbortSignal) => request<SessionView>(sessionPath(id), signal),
    branch: (id: string, checkpoint: Checkpoint, request_id: string) =>
      request<{ session: Session }>(`${sessionPath(id)}/branch`, undefined, { checkpoint, request_id }),
    exportConversation: async (id: string, format: ExportFormat, diagnostics: boolean) =>
      (await fetchResponse(`${sessionPath(id)}/export?format=${format}&diagnostics=${Number(diagnostics)}`)).blob(),
    handoffDraft: (id: string, signal?: AbortSignal) => request<HandoffDraft>(`${sessionPath(id)}/handoff`, signal),
    startHandoff: (id: string, body: HandoffDetails & { confirmed: true; request_id: string }) =>
      request<{ task_id: string }>(`${sessionPath(id)}/handoff`, undefined, body),
    linkedOutputs: (id: string, task: string, signal?: AbortSignal) =>
      request<LinkedOutputs>(`${sessionPath(id)}/artifacts/${encodeURIComponent(task)}`, signal),
    live: (id: string, cursor: number | null, signal: AbortSignal) =>
      request<LiveSnapshot>(`${sessionPath(id)}/events${cursor === null ? '' : `?after=${cursor}`}`, signal),
    send: (id: string, body: MessageSubmission) =>
      request<{ turn_id: number }>(`${sessionPath(id)}/messages`, undefined, body),
    startRun: (id: string, body: Pick<MessageSubmission, 'prompt' | 'request_id' | 'share_history'> & {
      context: string; participant?: number;
    }) =>
      request<{ run_id: string }>(`${sessionPath(id)}/messages`, undefined, body),
    runAction: (id: string, run: Pick<GroupRun, 'id' | 'revision'>, { kind, ...body }: RunAction) =>
      request<{ run_id: string; revision: number } | { session_id: string }>(
        `${sessionPath(id)}/runs/${encodeURIComponent(run.id)}/${kind}`, undefined, { ...body, revision: run.revision }),
    cancel: (id: string, turnId: number) =>
      request<{ turn_id: number }>(`${sessionPath(id)}/cancel`, undefined, { turn_id: turnId }),
    codeOverview: (signal?: AbortSignal) => request<CodeOverview>('/api/overview', signal),
    task: (id: string, signal?: AbortSignal) => request<TaskView>(taskPath(id), signal),
    taskChanges: (id: string, signal?: AbortSignal) => request<{ changes: TaskChanges }>(`${taskPath(id)}/diff`, signal),
    createTask: (prompt: string, recipient: Recipient, roles: AgentMap<Role>) =>
      request<{ task_id: string }>(tasks, undefined, { prompt, recipient, roles }),
    messageTask: (id: string, message: string, recipient: Recipient) =>
      request(`${taskPath(id)}/message`, undefined, { message, recipient }),
    taskAction: (id: string, action: TaskAction) => request(`${taskPath(id)}/${action}`, undefined, {}),
    quit: () => request('/api/shutdown', undefined, {}),
  };
}

export type Api = ReturnType<typeof createApi>;

export const errorText = (error: unknown) => error instanceof Error ? error.message : 'Something went wrong.';
