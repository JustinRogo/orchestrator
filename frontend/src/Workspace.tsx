import { useLayoutEffect, useRef, type ReactNode } from 'react';
import type { ModelChoice, Provider, SessionSummary, Strategy } from './api';

export const strategyNames: Record<Strategy, string> = {
  single: 'Chat', compare: 'Compare', council: 'AI Council', roundtable: 'Roundtable',
  relay: 'Collaborative Relay', debate: 'Structured Debate',
};

export const modelKey = (model: ModelChoice) => `${model.provider}/${model.model}`;

export function availableModels(providers: Provider[]): ModelChoice[] {
  return providers.flatMap(provider => provider.configured
    ? provider.models.map(model => ({ provider: provider.id, model }))
    : []);
}

export function providerName(providers: Provider[], id: string): string {
  return providers.find(provider => provider.id === id)?.name
    ?? ({ openai: 'OpenAI', anthropic: 'Anthropic', google: 'Google' } as Record<string, string>)[id]
    ?? id;
}

export function ModelPicker({ id, label, providers, selected, onSelect, disabled, optional }: {
  id: string;
  label: string;
  providers: Provider[];
  selected: ModelChoice | null;
  onSelect: (model: ModelChoice | null) => void;
  disabled?: boolean;
  optional?: boolean;
}) {
  const available = availableModels(providers);
  const value = selected ? modelKey(selected) : '';
  const exists = selected && providers.some(provider => provider.id === selected.provider && provider.models.includes(selected.model));

  return <div className="field">
    <label htmlFor={id}>{label}</label>
    <select id={id} value={value} disabled={disabled || available.length === 0} onChange={event => {
      const next = available.find(model => modelKey(model) === event.target.value);
      if (next || optional) onSelect(next ?? null);
    }}>
      {(!selected || optional) && <option value="">{!available.length ? 'No configured models' : optional ? 'None' : 'Select a model'}</option>}
      {selected && !exists && <option value={value} disabled>{selected.model} · unavailable</option>}
      {providers.filter(provider => provider.models.length > 0).map(provider => <optgroup
        key={provider.id} label={provider.name + (provider.configured ? '' : ' · API key required')}>
        {provider.models.map(model => <option key={model} value={modelKey({ provider: provider.id, model })} disabled={!provider.configured}>
          {model}
        </option>)}
      </optgroup>)}
    </select>
  </div>;
}

export function SessionSidebar({ sessions, selected, error }: {
  sessions?: SessionSummary[];
  selected: string | null;
  error: string;
}) {
  return <aside className="sidebar" aria-label="Conversation history">
    <div className="sidebar-head">
      <div className="brand"><span className="brand-mark" aria-hidden="true">A</span>AI Team</div>
      <a href="#new" className="button new-chat">New conversation</a>
    </div>
    <h2>History</h2>
    <nav className="history" aria-label="Saved conversations">
      {error && <p className="error" role="alert">{error}</p>}
      {!error && !sessions && <p className="muted" role="status">Loading history…</p>}
      {sessions?.length === 0 && <p className="muted">No conversations yet.</p>}
      {sessions?.map(session => {
        const prefix = session.strategy === 'single' ? '' : `${strategyNames[session.strategy]} · `;
        return <a key={session.id} href={`#${session.id}`} title={`${prefix}${session.title}`}
          aria-current={session.id === selected ? 'page' : undefined}>{prefix}{session.title}</a>;
      })}
    </nav>
  </aside>;
}

export function WorkspaceHeader({ codeMode, onQuit, quitting, session, project, codeWorkspace }: {
  codeMode: boolean;
  onQuit: () => void;
  quitting: boolean;
  session?: SessionSummary;
  project?: { name: string; path: string };
  codeWorkspace?: boolean;
}) {
  return <header className="workspace-header">
    <div className="workspace-title">
      <strong>{codeWorkspace ? 'Code Mode' : session && session.strategy !== 'single' ? strategyNames[session.strategy] : 'Chat Mode'}</strong>
      {project && <span className="muted" title={project.path}>{project.name}</span>}
    </div>
    <nav aria-label="Workspace modes">
      {codeWorkspace ? <>
        <a href="/chat">Chat Mode</a>
        <a href="/classic">Classic</a>
      </> : <>
        <a href={session && ['single', 'compare', 'council'].includes(session.strategy) ? `/classic/chat#${session.id}` : '/classic/chat'}>Classic Chat</a>
        {codeMode && <a href="/">Code Mode</a>}
      </>}
      <button type="button" onClick={onQuit} disabled={quitting}>Quit</button>
    </nav>
  </header>;
}

export function ConversationPane({ title, wide, follow, children }: {
  title: string;
  wide?: boolean;
  follow: boolean;
  children: ReactNode;
}) {
  const scroll = useRef<HTMLDivElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const atEnd = useRef(true);

  useLayoutEffect(() => heading.current?.focus({ preventScroll: true }), []);
  useLayoutEffect(() => {
    if (follow && atEnd.current && scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight;
  }, [follow, children]);

  return <div className="content-scroll" ref={scroll} onScroll={event => {
    const { scrollHeight, scrollTop, clientHeight } = event.currentTarget;
    atEnd.current = scrollHeight - scrollTop - clientHeight < 80;
  }}>
    <div className={`measure${wide ? ' wide' : ''}`}>
      <h1 ref={heading} tabIndex={-1}>{title}</h1>
      {children}
    </div>
  </div>;
}

export function MessageCard({ author, content, variant, metadata, pending, heading: Heading = 'h2' }: {
  author: string;
  content: string;
  variant: 'user' | 'assistant';
  metadata?: string;
  pending?: boolean;
  heading?: 'h2' | 'h3' | 'h4';
}) {
  return <article className={`message ${variant}`} data-pending={pending || undefined}>
    <Heading className="message-author">{author}</Heading>
    <p className="message-text">{content}</p>
    {metadata && <p className="muted metadata">{metadata}</p>}
  </article>;
}
