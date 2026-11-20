import { useCallback, useEffect, useState } from 'react';
import { errorText, type Api } from './api';
import { CodeComposer } from './CodeComposer';
import { CodeInspector } from './CodeInspector';
import { agentName, displayedMessage, taskState } from './codeText';
import { agentNames, type Activity, type AgentMap, type CodeMessage, type CodeOverview, type Recipient, type Role, type TaskAction } from './codeTypes';
import { usePolling } from './usePolling';
import { ConversationPane, WorkspaceHeader } from './Workspace';

function taskFromUrl() {
  const hash = window.location.hash.slice(1);
  return /^[a-f0-9]{12}$/.test(hash) ? hash : hash ? null : undefined;
}

function statusText(status: string, active: boolean, activity?: Activity | null) {
  if (activity?.agent) return `${agentName(activity.agent)} ${activity.stage}`;
  return active ? 'Starting' : taskState(status, false);
}

function CodeSidebar({ overview, selected, error }: { overview?: CodeOverview; selected?: string | null; error: string }) {
  return <aside className="sidebar code-sidebar" aria-label="Code tasks">
    <div className="sidebar-head">
      <div className="brand"><span className="brand-mark" aria-hidden="true">A</span>AI Team</div>
      <a href="#new" className="button new-chat">New task</a>
    </div>
    <h2>Tasks</h2>
    <nav className="history code-history" aria-label="Saved tasks">
      {error && <p className="error" role="alert">{error}</p>}
      {!overview && !error && <p className="muted" role="status">Loading tasks…</p>}
      {overview?.tasks.length === 0 && <p className="muted">No tasks yet.</p>}
      {overview?.tasks.map(task => <a key={task.id} href={`#${task.id}`} title={task.title}
        aria-current={selected === task.id ? 'page' : undefined}>
        <span className="task-title">{task.title}</span>
        <span className="task-meta muted">{statusText(task.status, task.active, overview.activity[task.id])} · {task.turn_count} turns</span>
      </a>)}
    </nav>
    {overview && <details className="code-agents" open>
      <summary>Agents</summary>
      {agentNames.filter(name => overview.agents[name] !== undefined).map(name => {
        const active = Object.values(overview.activity).some(activity => activity.agent === name);
        const used = overview.usage[name];
        const quota = overview.quotas[name];
        const windows = quota?.windows ?? [];
        const gemini = windows.filter(window => /^gemini/i.test(window.group ?? ''));
        const remaining = (name === 'gemini' && gemini.length ? gemini : windows)
          .map(window => `${window.label} ${window.remaining_percent}%`).join(' · ');
        return <div className="agent-row" key={name}>
          <div className="agent-name"><strong>{agentName(name)}</strong><span className="muted">{active ? 'Active' : overview.agents[name] ? 'Ready' : 'Missing'}</span></div>
          <p className="muted">Latest run: {used == null ? 'unavailable' : `${used.toLocaleString()} tokens`}</p>
          <p className="muted" title={quota?.checked_at ? `${quota.source} · Checked ${new Date(quota.checked_at).toLocaleString()}` : undefined}>
            Remaining: {remaining || 'unavailable'}
          </p>
        </div>;
      })}
    </details>}
  </aside>;
}

function CodeTranscript({ messages }: { messages: CodeMessage[] }) {
  if (!messages.length) return <p className="muted">No messages yet.</p>;
  return <div aria-label="Task conversation">
    {messages.map(message => <article className={`message ${message.sender === 'human' ? 'user' : 'assistant'}`} key={message.id}>
      <div className="code-message-heading">
        <h2 className="message-author">{agentName(message.sender)}</h2>
        {message.recipient && message.recipient !== 'all' && <span className="muted">to {agentName(message.recipient)}</span>}
        <time className="muted" dateTime={message.timestamp}>
          {new Date(message.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
        </time>
      </div>
      <p className="message-text">{displayedMessage(message.content)}</p>
    </article>)}
  </div>;
}

export function CodeWorkspace({ api }: { api: Api }) {
  const [selected, setSelected] = useState(taskFromUrl);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [stopped, setStopped] = useState(false);

  const readOverview = useCallback((_: string, signal: AbortSignal) => api.codeOverview(signal), [api]);
  const overview = usePolling(stopped ? null : 'overview', readOverview);
  const detail = usePolling(stopped ? null : selected ?? null, api.task);
  const diff = usePolling(stopped ? null : selected ?? null, api.taskChanges);
  const task = detail.data?.task;
  const draftKey = selected ?? 'new';
  const prompt = drafts[draftKey] ?? '';

  useEffect(() => {
    const onHash = () => { setSelected(taskFromUrl()); setError(''); };
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  useEffect(() => {
    if (!overview.data) return;
    document.title = `AI Team · ${overview.data.project.name}`;
    if (selected !== undefined) return;
    const attention = overview.data.tasks.find(item => item.active || ['running', 'awaiting_human', 'failed'].includes(item.status));
    window.history.replaceState(null, '', `#${attention?.id ?? 'new'}`);
    setSelected(attention?.id ?? null);
  }, [overview.data, selected]);

  function refresh() {
    overview.refresh();
    detail.refresh();
    diff.refresh();
  }

  async function send(message: string, recipient: Recipient, roles: AgentMap<Role>) {
    setBusy(true);
    setError('');
    try {
      if (selected) await api.messageTask(selected, message, recipient);
      else {
        const { task_id } = await api.createTask(message, recipient, roles);
        if (taskFromUrl() === null) window.location.hash = task_id;
      }
      setDrafts(previous => previous[draftKey] === prompt ? { ...previous, [draftKey]: '' } : previous);
      refresh();
    } finally { setBusy(false); }
  }

  async function action(name: TaskAction) {
    if (!selected || busy) return;
    setBusy(true);
    setError('');
    try { await api.taskAction(selected, name); }
    catch (error) { setError(errorText(error)); }
    finally { setBusy(false); refresh(); }
  }

  async function quit() {
    const active = overview.data?.tasks.filter(item => item.active).length ?? 0;
    const warning = active ? `${active} active ${active === 1 ? 'task will' : 'tasks will'} be interrupted. ` : '';
    if (!window.confirm(`${warning}Stop the AI Team server?`)) return;
    setBusy(true);
    setError('');
    try { await api.quit(); setStopped(true); }
    catch (error) { setError(errorText(error)); }
    finally { setBusy(false); }
  }

  if (stopped) return <main className="stopped"><h1>AI Team has stopped</h1><p className="muted">You can close this tab.</p></main>;

  return <div className="workspace code-workspace">
    <CodeSidebar overview={overview.data} selected={selected} error={overview.error} />
    <main className="workarea code-workarea">
      <WorkspaceHeader codeMode codeWorkspace project={overview.data?.project} quitting={busy} onQuit={() => void quit()} />
      {error && <div className="workspace-notice" role="alert">{error}</div>}
      <ConversationPane key={`transcript-${draftKey}`} title={task?.title ?? (selected ? 'Task' : 'New task')} follow={Boolean(task?.active)}>
        {selected ? <>
          {detail.error && <p className="error" role="alert">{detail.error}</p>}
          {task ? <>
            {detail.data?.source && <p className="task-source"><a href={'/chat#' + detail.data.source.id}>
              Source conversation · {detail.data.source.title}</a></p>}
            <p className="muted" role="status">{statusText(task.status, task.active, task.activity)} · {task.turn_count} agent turns</p>
            <div className="code-actions">
              <button type="button" disabled={busy || task.active || !task.can_retry} onClick={() => void action('retry')}>Retry failed turn</button>
              <button type="button" disabled={busy || task.active || task.status !== 'running'} onClick={() => void action('resume')}>Resume</button>
              <button className="danger" type="button" disabled={busy || task.status !== 'running'} onClick={() => void action('stop')}>Stop after current turn</button>
              <button type="button" onClick={refresh}>Refresh</button>
              <button type="button" className="details-jump" onClick={() => document.getElementById('task-details')?.focus()}>Task details</button>
            </div>
            <CodeTranscript messages={detail.data?.messages ?? []} />
          </> : !detail.error && <p className="muted" role="status">Loading task…</p>}
        </> : <p className="muted">Select agent roles and start a coding task with one agent or the whole team.</p>}
      </ConversationPane>
      <CodeComposer key={`composer-${draftKey}`} task={task} isNew={selected === null} overview={overview.data} prompt={prompt}
        setPrompt={value => setDrafts(previous => ({ ...previous, [draftKey]: value }))} busy={busy} onSend={send} />
    </main>
    <CodeInspector task={task} overview={overview.data} changes={diff.data?.changes} diffError={diff.error} />
  </div>;
}
