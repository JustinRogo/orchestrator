export const agentNames = ['codex', 'claude', 'gemini'] as const;
export type Agent = typeof agentNames[number];
export type Recipient = Agent | 'all';
export type Role = 'implementation' | 'q&a' | 'review' | 'qa';
export type TaskAction = 'retry' | 'resume' | 'stop';
export type AgentMap<T> = Partial<Record<Agent, T>>;

export interface Activity {
  agent: Agent | '';
  stage: string;
}

export interface TaskSummary {
  id: string;
  title: string;
  status: string;
  turn_count: number;
  active: boolean;
}

export interface CodeOverview {
  project: { name: string; path: string };
  agents: AgentMap<boolean>;
  default_roles: AgentMap<string>;
  role_options: Record<Role, string>;
  activity: Record<string, Activity>;
  usage: AgentMap<number | null>;
  quotas: AgentMap<{
    windows?: { label: string; remaining_percent: number; group?: string }[];
    source: string;
    checked_at: string;
  }>;
  tasks: TaskSummary[];
}

type BudgetAmounts = { tokens: number | null; cost_usd: number | null };

export interface CodeTask extends TaskSummary {
  activity: Activity | null;
  can_retry: boolean;
  current_round: number;
  max_rounds: number;
  queue: { agent: Agent }[];
  roles: AgentMap<string>;
  budget_summary: { limit: BudgetAmounts; used: BudgetAmounts; remaining: BudgetAmounts };
  pause_reason: string | null;
  artifacts: { agent: Agent; tests?: { command?: string[]; exit_code?: number; error?: string }[] }[];
}

export interface CodeMessage {
  id: string;
  sender: string;
  recipient: string | null;
  timestamp: string;
  content: string;
}

export interface TaskView {
  task: CodeTask;
  messages: CodeMessage[];
  source?: { id: string; title: string } | null;
}

export type TaskChanges = AgentMap<{ files: string[]; diff: string; truncated: boolean }>;
