import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { errorText, isRunActive, newRequestId, type Api, type Brief, type Checkpoint, type CodeLink,
  type ExportFormat, type HandoffDraft, type LinkedOutputs, type SessionView } from './api';
import { agentName, displayedMessage, taskState } from './codeText';
import { agentNames, type Agent, type Recipient, type Role } from './codeTypes';

const fields: [keyof Brief, string][] = [
  ['objective', 'Objective'], ['requirements', 'Requirements'], ['decisions', 'Decisions'],
  ['rejected_alternatives', 'Rejected alternatives'], ['uncertainties', 'Open questions and dissent'],
  ['acceptance_criteria', 'Acceptance criteria'], ['context', 'Reference context'],
];

function OutputDialog({ title, wide, busy, close, children }: {
  title: string; wide?: boolean; busy?: boolean; close: () => void; children: ReactNode;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    return () => element?.close();
  }, []);
  return <dialog ref={dialog} className={wide ? 'run-dialog output-dialog wide-dialog' : 'run-dialog output-dialog'}
    aria-labelledby="output-title" onCancel={event => {
      event.preventDefault();
      if (!busy) close();
    }}>
    <h2 id="output-title">{title}</h2>
    {children}
  </dialog>;
}

function BranchDialog({ api, id, points, onBranch, close }: {
  api: Api; id: string; points: { id: Checkpoint; label: string }[]; onBranch: (id: string) => void; close: () => void;
}) {
  const [choice, setChoice] = useState(String(points[0]?.id ?? ''));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const attempt = useRef<{ point: Checkpoint; id: string } | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const point = points.find(point => String(point.id) === choice);
    if (busy || !point) return;
    if (attempt.current?.point !== point.id) attempt.current = { point: point.id, id: newRequestId() };
    setBusy(true);
    setError('');
    try {
      const { session } = await api.branch(id, point.id, attempt.current.id);
      close();
      onBranch(session.id);
    } catch (error) {
      setError(errorText(error));
    } finally {
      setBusy(false);
    }
  }

  return <OutputDialog title="Branch conversation" busy={busy} close={close}>
    <form onSubmit={submit}>
      <p className="muted">Copy the history through a saved turn or run, then continue in a new conversation.</p>
      <div className="field">
        <label htmlFor="branch-point">Branch through</label>
        <select id="branch-point" value={choice} disabled={busy} onChange={event => setChoice(event.target.value)}>
          {points.map(point => <option value={point.id} key={point.id}>{point.label}</option>)}
        </select>
      </div>
      {error && <p className="error" role="alert">{error}</p>}
      <div className="actions">
        <button type="button" disabled={busy} onClick={close}>Cancel</button>
        <button className="primary" type="submit" disabled={busy || !choice}>{busy ? 'Branching…' : 'Create branch'}</button>
      </div>
    </form>
  </OutputDialog>;
}

function ExportDialog({ api, id, close }: { api: Api; id: string; close: () => void }) {
  const [format, setFormat] = useState<ExportFormat>('markdown');
  const [diagnostics, setDiagnostics] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError('');
    try {
      const file = await api.exportConversation(id, format, diagnostics);
      const url = URL.createObjectURL(file);
      const link = document.createElement('a');
      link.href = url;
      link.download = 'conversation-' + id + (format === 'json' ? '.json' : '.md');
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      close();
    } catch (error) {
      setError(errorText(error));
    } finally {
      setBusy(false);
    }
  }

  return <OutputDialog title="Export conversation" busy={busy} close={close}>
    <form onSubmit={submit}>
      <p className="muted">The file can contain private prompts, replies, and instructions. Review it before sharing.</p>
      <div className="field">
        <label htmlFor="export-format">Format</label>
        <select id="export-format" value={format} disabled={busy} onChange={event => setFormat(event.target.value as ExportFormat)}>
          <option value="markdown">Markdown</option><option value="json">JSON</option>
        </select>
      </div>
      <label className="consent output-confirm">
        <input type="checkbox" checked={diagnostics} disabled={busy} onChange={event => setDiagnostics(event.target.checked)} />
        Include usage, errors, and truncation details
      </label>
      {error && <p className="error" role="alert">{error}</p>}
      <div className="actions">
        <button type="button" disabled={busy} onClick={close}>Cancel</button>
        <button className="primary" type="submit" disabled={busy}>{busy ? 'Preparing…' : 'Download'}</button>
      </div>
    </form>
  </OutputDialog>;
}

function HandoffDialog({ api, id, close }: { api: Api; id: string; close: () => void }) {
  const [draft, setDraft] = useState<HandoffDraft>();
  const [error, setError] = useState('');
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [taskId, setTaskId] = useState('');
  const attempt = useRef<{ key: string; id: string } | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    void api.handoffDraft(id, controller.signal).then(setDraft).catch(error => {
      if (!controller.signal.aborted) setError(errorText(error));
    });
    return () => controller.abort();
  }, [api, id]);

  function edit(next: HandoffDraft) {
    setDraft(next);
    setConfirmed(false);
  }

  function assignRole(agent: Agent, role: Role) {
    if (!draft) return;
    const roles = { ...draft.roles };
    if (role === 'implementation') {
      for (const other of agentNames) {
        if (other !== agent && roles[other] === 'implementation') roles[other] = 'review';
      }
    }
    roles[agent] = role;
    edit({ ...draft, roles });
  }

  const enabled = agentNames.filter(agent => draft?.agents[agent] !== undefined);
  const ready = enabled.filter(agent => draft?.agents[agent] === true);
  const canSend = draft && ready.length && confirmed && draft.brief.objective.trim() && !busy;
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!draft || !canSend) return;
    const { brief, repository, roles, recipient } = draft;
    const payload = { brief, repository, roles, recipient };
    const key = JSON.stringify(payload);
    if (attempt.current?.key !== key) attempt.current = { key, id: newRequestId() };
    setBusy(true);
    setError('');
    try {
      const { task_id } = await api.startHandoff(id, { ...payload, request_id: attempt.current.id, confirmed: true });
      setTaskId(task_id);
    } catch (error) {
      setError(errorText(error));
    } finally {
      setBusy(false);
    }
  }

  return <OutputDialog title="Turn into Coding Task" wide busy={busy} close={close}>
    {taskId ? <>
      <p role="status">The task has started in Code Mode.</p>
      <div className="actions"><button type="button" onClick={close}>Close</button>
        <a className="button primary" href={'/#' + taskId}>Open Code task</a></div>
    </> : <form onSubmit={submit}>
      {!draft && !error && <p className="muted" role="status">Preparing the brief…</p>}
      {draft && <>
        <p className="muted">Review and edit the draft before starting. It was prepared from the saved discussion.</p>
        <dl className="handoff-repository"><dt>Target repository</dt><dd>{draft.repository}</dd></dl>
        <div className="brief-grid">
          {fields.map(([key, label]) => <div className="field" key={key}>
            <label htmlFor={'brief-' + key}>{label}</label>
            <textarea id={'brief-' + key} rows={key === 'context' ? 4 : 2} maxLength={4500}
              value={draft.brief[key]} required={key === 'objective'} disabled={busy}
              onChange={event => edit({ ...draft, brief: { ...draft.brief, [key]: event.target.value } })} />
          </div>)}
        </div>
        {enabled.length > 0 && <fieldset className="code-roles">
          <legend>Agent roles</legend>
          <div>{enabled.map(agent => <div className="field" key={agent}>
            <label htmlFor={'handoff-role-' + agent}>{agentName(agent)}</label>
            <select id={'handoff-role-' + agent} value={draft.roles[agent]} disabled={busy}
              onChange={event => assignRole(agent, event.target.value as Role)}>
              {Object.entries(draft.role_options).map(([role, label]) => <option key={role} value={role}>{label}</option>)}
            </select>
          </div>)}</div>
        </fieldset>}
        {ready.length ? <div className="field">
          <label htmlFor="handoff-recipient">Start with</label>
          <select id="handoff-recipient" value={draft.recipient} disabled={busy}
            onChange={event => edit({ ...draft, recipient: event.target.value as Recipient })}>
            {ready.length === enabled.length && <option value="all">Whole team</option>}
            {ready.map(agent => <option key={agent} value={agent}>{agentName(agent)}</option>)}
          </select>
        </div> : <p className="error">Configure an enabled Code Mode CLI to start this task.</p>}
        <label className="consent output-confirm">
          <input type="checkbox" checked={confirmed} disabled={busy || !ready.length}
            onChange={event => setConfirmed(event.target.checked)} />
          I reviewed the brief and repository. The implementor may write in an isolated Git worktree.
        </label>
      </>}
      {error && <p className="error" role="alert">{error}</p>}
      <div className="actions">
        <button type="button" disabled={busy} onClick={close}>Cancel</button>
        {draft && <button className="primary" type="submit" disabled={!canSend}>
          {busy ? 'Starting…' : 'Create and start Code task'}
        </button>}
      </div>
    </form>}
  </OutputDialog>;
}

function ArtifactsDialog({ api, id, link, codeMode, close }: {
  api: Api; id: string; link: CodeLink; codeMode: boolean; close: () => void;
}) {
  const [data, setData] = useState<LinkedOutputs>();
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    void api.linkedOutputs(id, link.task_id, controller.signal).then(data => {
      if (!controller.signal.aborted) setData(data);
    }).catch(error => {
      if (!controller.signal.aborted) setError(errorText(error));
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
    return () => controller.abort();
  }, [api, id, link.task_id, revision]);

  function refresh() {
    setError('');
    setLoading(true);
    setRevision(value => value + 1);
  }

  return <OutputDialog title="Linked Code artifacts" wide close={close}>
    <p className="muted">{link.title}{data && ' · ' + taskState(data.status, false)}</p>
    {loading && <p className="muted" role="status">Loading artifacts…</p>}
    {error && <p className="error" role="alert">{error}</p>}
    {data && <div className="artifacts">
      <details className="settings"><summary>Approved coding brief</summary>
        <p className="muted">{data.handoff.repository}</p>
        <dl>{fields.filter(([key]) => data.handoff.brief[key]).map(([key, label]) => <div key={key}>
          <dt>{label}</dt><dd className="message-text">{data.handoff.brief[key]}</dd>
        </div>)}</dl>
      </details>
      <details className="settings" open><summary>Agent updates and reviews</summary>
        {!data.updates.length && <p className="muted">No saved agent updates yet.</p>}
        {data.updates.map(update => <article key={update.id}>
          <h3>{agentName(update.agent)}</h3>
          <p className="message-text">{displayedMessage(update.content)}{update.truncated && '…'}</p>
        </article>)}
      </details>
      <details className="settings"><summary>Changed files and diffs</summary>
        {Object.entries(data.changes).every(([, change]) => !change.files.length && !change.diff)
          && <p className="muted">No changes in the saved agent runs.</p>}
        {Object.entries(data.changes).filter(([, change]) => change.files.length || change.diff).map(([agent, change]) => <article key={agent}>
          <h3>{agentName(agent)}</h3>
          {change.files.length > 0 && <ul>{change.files.map(file => <li key={file}>{file}</li>)}</ul>}
          {change.diff && <pre tabIndex={0} aria-label={agentName(agent) + ' Git diff'}>
            {change.diff}{change.truncated && '\n\n[Diff shortened]'}</pre>}
        </article>)}
      </details>
      <details className="settings"><summary>Test results</summary>
        {data.tests.length ? <ul className="test-results">{data.tests.map((test, index) => <li key={index}>
          <span>{agentName(test.agent)} · {test.command?.join(' ') || 'Test'}</span>
          <span className={test.error || test.exit_code !== 0 ? 'error' : 'test-passed'}>
            {test.error ? 'Error' : test.exit_code === 0 ? 'Passed' : 'Failed'}</span>
        </li>)}</ul> : <p className="muted">No configured tests have been recorded.</p>}
      </details>
    </div>}
    <div className="actions">
      <button type="button" onClick={close}>Close</button>
      <button type="button" disabled={loading} onClick={refresh}>Refresh artifacts</button>
      {codeMode && <a className="button" href={'/#' + link.task_id}>Open Code task</a>}
    </div>
  </OutputDialog>;
}

export function ConversationTools({ api, view, codeMode, onBranch }: {
  api: Api; view: SessionView; codeMode: boolean; onBranch: (id: string) => void;
}) {
  const [dialog, setDialog] = useState<'branch' | 'export' | 'handoff' | CodeLink | null>(null);
  const { id, branch_source: source } = view.session;
  const prefix = 'turns' in view ? 'Turn' : 'Run';
  const entries = 'turns' in view ? view.turns : view.runs;
  const firstActive = entries.findIndex(item => 'stages' in item ? isRunActive(item) : item.status === 'pending');
  const points = entries.slice(0, firstActive === -1 ? entries.length : firstActive)
    .map((item, index) => ({ id: item.id, label: prefix + ' ' + (index + 1) + ' · ' + item.prompt.split('\n')[0].slice(0, 85) }))
    .reverse();

  function close() {
    setDialog(null);
  }

  return <div className="conversation-tools">
    <details className="measure wide settings">
      <summary>Conversation tools{view.handoffs.length > 0 && ' · ' + view.handoffs.length + ' linked'}</summary>
      <div className="conversation-tool-actions">
        <button type="button" disabled={!points.length} onClick={() => setDialog('branch')}>Branch conversation</button>
        <button type="button" onClick={() => setDialog('export')}>Export conversation</button>
        {codeMode && <button type="button" onClick={() => setDialog('handoff')}>Turn into Coding Task</button>}
      </div>
      {source && <a className="source-link" href={'#' + source.session_id}>Source conversation · {source.title}</a>}
      {view.handoffs.length > 0 && <ul className="linked-tasks">
        {view.handoffs.map(link => <li key={link.task_id}>
          {codeMode ? <a href={'/#' + link.task_id}>{link.title}</a> : <span>{link.title}</span>}
          <button type="button" onClick={() => setDialog(link)}>View artifacts</button>
        </li>)}
      </ul>}
    </details>
    {dialog === 'branch' && <BranchDialog api={api} id={id} points={points} onBranch={onBranch} close={close} />}
    {dialog === 'export' && <ExportDialog api={api} id={id} close={close} />}
    {dialog === 'handoff' && <HandoffDialog api={api} id={id} close={close} />}
    {dialog && typeof dialog === 'object' && <ArtifactsDialog api={api} id={id} link={dialog} codeMode={codeMode} close={close} />}
  </div>;
}
