import { useRef, useState, type FormEvent } from 'react';
import { errorText, newRequestId, type Api, type ChatView, type ModelChoice, type Provider } from './api';
import { availableModels, ConversationPane, MessageCard, modelKey, ModelPicker, providerName } from './Workspace';

function ConversationTimeline({ view, providers, drafts }: {
  view: ChatView;
  providers: Provider[];
  drafts: Record<string, string>;
}) {
  const { session, turns } = view;
  return <ConversationPane title={session.title} follow={turns.length > 0}>
      <details className="settings conversation-settings">
        <summary>Conversation settings</summary>
        <p className="muted">{session.max_turns} prompts · {session.max_output_tokens} output tokens per reply · {session.timeout_seconds}s timeout</p>
        {session.system_prompt && <p className="message-text">{session.system_prompt}</p>}
      </details>
      {turns.length === 0 && <p className="muted">Send a message to begin.</p>}
      <div aria-label="Conversation messages">
        {turns.map(turn => {
          const pending = turn.status === 'pending';
          const usage = turn.total_tokens === null ? 'Total tokens unavailable' : `${turn.total_tokens.toLocaleString()} tokens`;
          const details = [usage, 'Cost unknown'];
          if (turn.context_truncated) details.push('Context shortened');
          if (turn.output_limited) details.push('Output limit reached');
          const content = turn.response ?? turn.error ?? drafts[`turn:${turn.id}`] ?? 'Waiting for a response…';
          return <div className="turn" key={turn.id}>
            <MessageCard author="You" content={turn.prompt} variant="user" />
            <MessageCard author={`${providerName(providers, turn.provider)} · ${turn.response_model ?? turn.model}`}
              content={content} variant="assistant" pending={pending}
              metadata={pending ? 'Reply in progress' : turn.status === 'complete' || turn.total_tokens !== null ? details.join(' · ') : undefined} />
          </div>;
        })}
      </div>
  </ConversationPane>;
}

function Composer({ view, providers, connectionStatus, send, cancel }: {
  view: ChatView;
  providers: Provider[];
  connectionStatus: string;
  send: (model: ModelChoice, prompt: string, requestId: string, shareHistory: boolean) => Promise<void>;
  cancel: (turnId: number) => Promise<void>;
}) {
  const [selection, setSelection] = useState<ModelChoice | null>(null);
  const [prompt, setPrompt] = useState('');
  const [shareHistory, setShareHistory] = useState(false);
  const [sending, setSending] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [error, setError] = useState('');
  const retry = useRef<{ key: string; id: string } | null>(null);

  const { session, turns } = view;
  const model = selection ?? { provider: session.provider, model: session.model };
  const active = turns.at(-1)?.status === 'pending' ? turns.at(-1) : undefined;
  const available = availableModels(providers).some(choice => modelKey(choice) === modelKey(model));
  const crossProvider = view.context_providers.some(provider => provider !== model.provider);
  const limited = turns.length - (session.branch_source?.history_count ?? 0) >= session.max_turns;
  const canSend = available && !sending && !active && !limited && (!crossProvider || shareHistory) && Boolean(prompt.trim());

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSend) return;
    const submitted = prompt.trim();
    const key = JSON.stringify([model.provider, model.model, submitted]);
    if (retry.current?.key !== key) retry.current = { key, id: newRequestId() };
    setError('');
    setSending(true);
    try {
      await send(model, submitted, retry.current.id, shareHistory);
      setPrompt(value => value.trim() === submitted ? '' : value);
      retry.current = null;
    } catch (error) {
      setError(errorText(error));
    } finally {
      setSending(false);
    }
  }

  async function cancelReply(turnId: number) {
    setError('');
    setCancelling(true);
    try { await cancel(turnId); }
    catch (error) { setError(errorText(error)); }
    finally { setCancelling(false); }
  }

  const status = connectionStatus || (active || sending ? 'Reply in progress…'
    : limited ? 'Prompt limit reached. Start a new conversation to continue.'
    : !available ? 'Choose a model with a configured API key.'
    : 'The selected provider receives this message and recent completed exchanges.');

  return <div className="composer">
    <form className="measure" onSubmit={submit}>
      <ModelPicker id="next-model" label="Model for next reply" selected={model} providers={providers}
        disabled={Boolean(active) || sending || limited} onSelect={choice => { setSelection(choice); setShareHistory(false); }} />
      {crossProvider && <label className="consent">
        <input type="checkbox" checked={shareHistory} onChange={event => setShareHistory(event.target.checked)} />
        Share recent conversation history with {providerName(providers, model.provider)}
      </label>}
      <div className="field">
        <label htmlFor="message">Message</label>
        <textarea id="message" rows={3} maxLength={12000} value={prompt} disabled={limited}
          onChange={event => setPrompt(event.target.value)} onKeyDown={event => {
            if (event.key === 'Enter' && (event.ctrlKey || event.metaKey) && canSend) {
              event.preventDefault();
              event.currentTarget.form?.requestSubmit();
            }
          }} />
      </div>
      {error && <p className="error" role="alert">{error}</p>}
      <div className="composer-footer">
        <p className="muted" role="status">{status}</p>
        <div className="actions">
          {active && <button type="button" className="danger" disabled={cancelling}
            title="Cancel the local request. The provider may still charge for work it received."
            onClick={() => void cancelReply(active.id)}>{cancelling ? 'Cancelling…' : 'Cancel reply'}</button>}
          <button className="primary" disabled={!canSend} type="submit">Send message</button>
        </div>
      </div>
    </form>
  </div>;
}

export function ChatConversation({ api, view, drafts, providers, connectionStatus, reload, onActivity }: {
  api: Api;
  view: ChatView;
  drafts: Record<string, string>;
  providers: Provider[];
  connectionStatus: string;
  reload: () => Promise<unknown>;
  onActivity: () => void;
}) {
  const { id } = view.session;
  return <>
    <ConversationTimeline view={view} providers={providers} drafts={drafts} />
    <Composer view={view} providers={providers} connectionStatus={connectionStatus}
      send={async (model, prompt, requestId, shareHistory) => {
        await api.send(id, { ...model, prompt, request_id: requestId, share_history: shareHistory });
        onActivity();
        await reload().catch(() => undefined);
      }} cancel={async turnId => {
        await api.cancel(id, turnId);
        await reload().catch(() => undefined);
      }} />
  </>;
}
