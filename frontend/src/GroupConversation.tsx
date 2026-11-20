import { useEffect, useRef, useState, type FormEvent } from 'react';
import { errorText, isRunActive, newRequestId, type Api, type GroupRun, type GroupView, type Participant, type Provider,
  type RunAction } from './api';
import { GroupTimeline, type Confirmation } from './GroupTimeline';
import { providerName } from './Workspace';

interface DialogAction extends Confirmation {
  run: GroupRun;
  action: RunAction;
}

function SpeakerPicker({ id, label, speakers, providers, value, onChange, disabled }: {
  id: string;
  label: string;
  speakers: Participant[];
  providers: Provider[];
  value: string;
  onChange: (value: string) => void;
  disabled: boolean;
}) {
  return <div className="field">
    <label htmlFor={id}>{label}</label>
    <select id={id} value={value} onChange={event => onChange(event.target.value)} disabled={disabled}>
      <option value="">Use discussion order or @mention</option>
      {speakers.map((model, index) => <option key={index} value={index}>
        @{providerName(providers, model.provider)} · {model.model}{model.role ? ` · ${model.role}` : ''}
      </option>)}
    </select>
  </div>;
}

function RunDialog({ choice, busy, onDismiss, onConfirm, speakers, providers }: {
  choice: DialogAction;
  busy: boolean;
  onDismiss: () => void;
  onConfirm: (action: RunAction) => Promise<void>;
  speakers: Participant[];
  providers: Provider[];
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [instruction, setInstruction] = useState('');
  const [speaker, setSpeaker] = useState('');
  const [error, setError] = useState('');
  const expand = choice.action.kind === 'expand';
  const direct = choice.action.kind === 'next';

  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    return () => element?.close();
  }, []);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    const action = choice.action;
    if (action.kind === 'expand' && !instruction.trim()) return;
    setError('');
    try {
      if (action.kind === 'next') {
        await onConfirm({ ...action, instruction: instruction.trim(), participant: speaker === '' ? undefined : Number(speaker) });
      } else {
        await onConfirm(action.kind === 'expand' ? { ...action, instruction: instruction.trim() } : action);
      }
      onDismiss();
    } catch (error) {
      setError(errorText(error));
    }
  }

  return <dialog ref={dialog} className="run-dialog" aria-labelledby="action-title" aria-describedby="action-description"
    onCancel={event => { event.preventDefault(); if (!busy) onDismiss(); }}>
    <form onSubmit={submit}>
      <h2 id="action-title">{choice.title}</h2>
      <p className="muted" id="action-description">{choice.description}</p>
      {direct && <SpeakerPicker id="next-speaker" label="Next speaker" speakers={speakers} providers={providers}
        value={speaker} onChange={setSpeaker} disabled={busy} />}
      {(expand || direct) && <div className="field">
        <label htmlFor="followup">{direct ? 'Direction (optional)' : 'Follow-up question'}</label>
        <textarea id="followup" rows={3} maxLength={4000} required={expand} value={instruction} disabled={busy}
          onChange={event => setInstruction(event.target.value)} />
      </div>}
      {error && <p className="error" role="alert">{error}</p>}
      <div className="actions">
        <button type="button" disabled={busy} onClick={onDismiss}>Cancel</button>
        <button className="primary" type="submit" disabled={busy || (expand && !instruction.trim())}>
          {busy ? 'Working…' : expand ? 'Send follow-up' : direct ? 'Start turn' : 'Share replies'}
        </button>
      </div>
    </form>
  </dialog>;
}

function GroupComposer({ view, providers, connectionStatus, busy, startRun, actionError }: {
  view: GroupView;
  connectionStatus: string;
  busy: boolean;
  providers: Provider[];
  startRun: (prompt: string, context: string, shareHistory: boolean, requestId: string, participant?: number) => Promise<void>;
  actionError: string;
}) {
  const [prompt, setPrompt] = useState('');
  const [context, setContext] = useState('');
  const [shareHistory, setShareHistory] = useState(false);
  const [speaker, setSpeaker] = useState('');
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState('');
  const contextField = useRef<HTMLDetailsElement>(null);
  const retry = useRef<{ key: string; id: string } | null>(null);
  const { session, runs } = view;
  const shouldShare = session.strategy !== 'compare' && new Set(session.strategy_options.participants.map(item => item.provider)).size > 1;
  const active = runs.some(isRunActive);
  const limited = runs.length - (session.branch_source?.history_count ?? 0) >= session.max_turns;
  const canSend = !active && !sending && !busy && !limited && (!shouldShare || shareHistory) && Boolean(prompt.trim());
  const status = connectionStatus || (active || sending ? 'Run in progress…' : limited
    ? 'Prompt limit reached. Start a new conversation to continue.' : '');

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSend) return;
    const submitted = prompt.trim();
    const submittedContext = context.trim();
    const key = JSON.stringify([submitted, submittedContext, speaker]);
    if (retry.current?.key !== key) retry.current = { key, id: newRequestId() };
    setSendError('');
    setSending(true);
    try {
      await startRun(submitted, submittedContext, shareHistory, retry.current.id, speaker === '' ? undefined : Number(speaker));
      setPrompt(value => value.trim() === submitted ? '' : value);
      setContext(value => value.trim() === submittedContext ? '' : value);
      if (contextField.current && contextField.current.querySelector('textarea')?.value.trim() === submittedContext) {
        contextField.current.open = false;
      }
      retry.current = null;
    } catch (error) {
      setSendError(errorText(error));
    } finally {
      setSending(false);
    }
  }

  return <div className="composer">
    <form className="measure wide" onSubmit={submit}>
      <details ref={contextField} className="settings">
        <summary>Shared context (optional)</summary>
        <div className="field">
          <label htmlFor="shared-context">Context for all selected models</label>
          <textarea id="shared-context" rows={2} maxLength={12000} disabled={limited}
            value={context} onChange={event => setContext(event.target.value)} />
        </div>
      </details>
      {shouldShare && <label className="consent">
        <input type="checkbox" checked={shareHistory} onChange={event => setShareHistory(event.target.checked)} />
        Share {session.strategy === 'council' ? 'Council' : 'model'} replies across the selected providers
      </label>}
      {session.strategy === 'roundtable' && <SpeakerPicker id="opening-speaker" label="Opening speaker"
        speakers={session.strategy_options.participants} providers={providers} value={speaker} onChange={setSpeaker}
        disabled={limited || active || sending} />}
      <div className="field">
        <label htmlFor="shared-prompt">Shared prompt</label>
        <textarea id="shared-prompt" rows={3} maxLength={12000} disabled={limited}
          value={prompt} onChange={event => setPrompt(event.target.value)} onKeyDown={event => {
            if (event.key === 'Enter' && (event.ctrlKey || event.metaKey) && canSend) {
              event.preventDefault();
              event.currentTarget.form?.requestSubmit();
            }
          }} />
      </div>
      {(sendError || actionError) && <p className="error" role="alert">{actionError || sendError}</p>}
      <div className="composer-footer">
        <p className="muted" role="status">{status}</p>
        <div className="actions"><button className="primary" disabled={!canSend} type="submit">Start run</button></div>
      </div>
    </form>
  </div>;
}

export function GroupConversation({ api, view, providers, drafts, connectionStatus, reload, onActivity, onPromoted }: {
  api: Api;
  view: GroupView;
  providers: Provider[];
  drafts: Record<string, string>;
  connectionStatus: string;
  reload: () => Promise<unknown>;
  onActivity: () => void;
  onPromoted: (id: string) => void;
}) {
  const [dialog, setDialog] = useState<DialogAction | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState('');
  const { id } = view.session;

  async function execute(run: GroupRun, action: RunAction) {
    setBusy(true);
    setActionError('');
    try {
      const result = await api.runAction(id, run, action);
      if ('session_id' in result) onPromoted(result.session_id);
      else await reload().catch(() => undefined);
    } catch (error) {
      await reload().catch(() => undefined);
      throw error;
    } finally {
      setBusy(false);
    }
  }

  return <>
    <GroupTimeline view={view} providers={providers} drafts={drafts} busy={busy} onAction={(run, action, confirmation) => {
      if (confirmation) setDialog({ ...confirmation, run, action });
      else void execute(run, action).catch(error => setActionError(errorText(error)));
    }} />
    <GroupComposer view={view} providers={providers} connectionStatus={connectionStatus} busy={busy} actionError={actionError}
      startRun={async (prompt, context, share_history, request_id, participant) => {
        setActionError('');
        await api.startRun(id, { prompt, context, share_history, request_id, participant });
        onActivity();
        await reload().catch(() => undefined);
      }} />
    {dialog && <RunDialog choice={dialog} busy={busy} onDismiss={() => setDialog(null)} onConfirm={action => execute(dialog.run, action)}
      speakers={view.session.strategy_options.participants} providers={providers} />}
  </>;
}
