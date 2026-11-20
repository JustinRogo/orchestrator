import { useEffect, useRef, useState, type FormEvent } from 'react';
import { errorText } from './api';
import { agentName, parseRecipient } from './codeText';
import { agentNames, type Agent, type AgentMap, type CodeOverview, type CodeTask, type Recipient, type Role } from './codeTypes';

export function CodeComposer({ task, isNew, overview, prompt, setPrompt, busy, onSend }: {
  task?: CodeTask;
  isNew: boolean;
  overview?: CodeOverview;
  prompt: string;
  setPrompt: (value: string) => void;
  busy: boolean;
  onSend: (message: string, recipient: Recipient, roles: AgentMap<Role>) => Promise<void>;
}) {
  const [recipient, setRecipient] = useState<Recipient>('all');
  const [roleOverrides, setRoleOverrides] = useState<AgentMap<Role>>({});
  const [suggest, setSuggest] = useState(false);
  const [error, setError] = useState('');
  const input = useRef<HTMLTextAreaElement>(null);
  const enabled = agentNames.filter(name => overview?.agents[name] !== undefined);
  const recipients: Recipient[] = ['all', ...enabled];
  const mention = prompt.match(/^@([a-z]*)$/i);
  const suggestions = suggest && mention ? recipients.filter(name => {
    const fragment = mention[1].toLowerCase();
    return agentName(name).toLowerCase().startsWith(fragment) || name.startsWith(fragment);
  }) : [];
  const canMessage = task && !task.active && ['awaiting_human', 'review_ready', 'complete', 'stopped'].includes(task.status);
  const canSend = !busy && Boolean(prompt.trim()) && (isNew ? enabled.length > 0 : canMessage);
  const label = isNew ? 'Start a new task' : task?.status === 'awaiting_human' ? 'Give this task direction' : 'Message this task';

  const roles: AgentMap<Role> = {};
  for (const name of enabled) {
    const configured = overview?.default_roles[name]?.toLowerCase() ?? '';
    roles[name] = roleOverrides[name] ?? (configured === 'implementation' ? 'implementation'
      : configured === 'q&a' ? 'q&a' : configured.includes('qa') ? 'qa' : 'review');
  }

  useEffect(() => { if (isNew) input.current?.focus({ preventScroll: true }); }, [isNew]);

  function changeRole(name: Agent, role: Role) {
    const next = { ...roleOverrides, [name]: role };
    if (role === 'implementation') {
      for (const other of enabled) {
        if (other !== name && roles[other] === 'implementation') next[other] = 'q&a';
      }
    }
    setRoleOverrides(next);
  }

  function chooseRecipient(name: Recipient) {
    setRecipient(name);
    setPrompt(`@${agentName(name)} `);
    setSuggest(false);
    input.current?.focus();
  }

  function editPrompt(value: string) {
    setPrompt(value);
    setSuggest(true);
    if (!value.trimStart().startsWith('@')) return;
    try {
      const mentioned = parseRecipient(value, recipient).recipient;
      if (recipients.includes(mentioned)) setRecipient(mentioned);
    } catch { /* A partial mention stays in the composer for autocomplete. */ }
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSend) return;
    setError('');
    try {
      const parsed = parseRecipient(prompt, recipient);
      if (!parsed.message) throw new Error('Enter a message after the recipient.');
      await onSend(parsed.message, parsed.recipient, roles);
      setSuggest(false);
    } catch (error) {
      setError(errorText(error));
    }
  }

  return <div className="composer code-composer">
    <form className="measure" onSubmit={submit}>
      {isNew && overview && enabled.length > 0 && <fieldset className="code-roles">
        <legend>Agent roles</legend>
        <div>
          {enabled.map(name => <div className="field" key={name}>
            <label htmlFor={`role-${name}`}>{agentName(name)}</label>
            <select id={`role-${name}`} value={roles[name]} disabled={busy}
              onChange={event => changeRole(name, event.target.value as Role)}>
              {Object.entries(overview.role_options).map(([key, text]) => <option key={key} value={key}>{text}</option>)}
            </select>
          </div>)}
        </div>
      </fieldset>}
      <div className="field">
        <label htmlFor="code-prompt">{label}</label>
        <textarea id="code-prompt" ref={input} rows={3} maxLength={20000} value={prompt}
          aria-describedby="code-hint" onChange={event => editPrompt(event.target.value)}
          onKeyDown={event => {
            if (event.key === 'Escape') setSuggest(false);
            if (event.key !== 'Enter') return;
            if (event.ctrlKey || event.metaKey) {
              event.preventDefault();
              if (canSend) event.currentTarget.form?.requestSubmit();
            } else if (suggestions.length > 0) {
              event.preventDefault();
              chooseRecipient(suggestions[0]);
            }
          }} />
      </div>
      {suggestions.length > 0 && <div className="recipient-suggestions" role="group" aria-label="Recipient suggestions">
        {suggestions.map(name => <button key={name} type="button" onClick={() => chooseRecipient(name)}>@{agentName(name)}</button>)}
      </div>}
      {error && <p className="error" role="alert">{error}</p>}
      <div className="composer-footer">
        <div className="recipient-control">
          <label htmlFor="code-recipient">To</label>
          <select id="code-recipient" value={recipient} onChange={event => {
            setRecipient(event.target.value as Recipient);
            setPrompt(/^\s*@[a-z]*$/i.test(prompt) ? '' : prompt.replace(/^\s*@(everyone|all|codex|claude|gemini|antigravity)(?:[\s,:-]+|$)/i, ''));
          }}>
            {recipients.map(name => <option key={name} value={name}>{agentName(name)}</option>)}
          </select>
        </div>
        <p id="code-hint" className="muted">Type @ to address an agent · Ctrl+Enter to send</p>
        <button className="primary" type="submit" disabled={!canSend}>{isNew ? 'Start task' : 'Send message'}</button>
      </div>
    </form>
  </div>;
}
