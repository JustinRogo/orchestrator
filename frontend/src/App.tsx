import { useCallback, useEffect, useRef, useState } from 'react';
import { errorText, type Api, type ModelCatalog, type Session, type SessionSummary } from './api';
import { Conversation } from './Conversation';
import { NewConversation } from './NewConversation';
import { SessionSidebar, WorkspaceHeader } from './Workspace';

function selectionFromUrl() {
  const id = window.location.hash.slice(1);
  return /^[a-f0-9]{32}$/.test(id) ? id : null;
}

export function App({ api, codeMode }: { api: Api; codeMode: boolean }) {
  const [selection, setSelection] = useState(selectionFromUrl);
  const [catalog, setCatalog] = useState<ModelCatalog>();
  const [sessions, setSessions] = useState<SessionSummary[]>();
  const [catalogError, setCatalogError] = useState('');
  const [historyError, setHistoryError] = useState('');
  const [quitError, setQuitError] = useState('');
  const [quitting, setQuitting] = useState(false);
  const [stopped, setStopped] = useState(false);
  const historyVersion = useRef(0);

  const refreshHistory = useCallback(async (signal?: AbortSignal) => {
    const version = ++historyVersion.current;
    try {
      const result = await api.listSessions(signal);
      if (!signal?.aborted && version === historyVersion.current) {
        setSessions(result.sessions);
        setHistoryError('');
      }
    } catch (error) {
      if (!signal?.aborted && version === historyVersion.current) setHistoryError(errorText(error));
    }
  }, [api]);

  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    void refreshHistory(signal);
    void api.models(signal).then(value => {
      if (!signal.aborted) setCatalog(value);
    }).catch(error => {
      if (!signal.aborted) setCatalogError(errorText(error));
    });
    return () => controller.abort();
  }, [api, refreshHistory]);

  useEffect(() => {
    const select = () => setSelection(selectionFromUrl());
    window.addEventListener('hashchange', select);
    return () => window.removeEventListener('hashchange', select);
  }, []);

  function openCreated(session: Session) {
    window.location.hash = session.id;
    void refreshHistory();
  }

  async function quit() {
    setQuitError('');
    setQuitting(true);
    try {
      await api.quit();
      setStopped(true);
    } catch (error) {
      setQuitError(errorText(error));
    } finally {
      setQuitting(false);
    }
  }

  if (stopped) return <main className="stopped">
    <h1>AI Team has stopped</h1>
    <p className="muted">You can close this tab.</p>
  </main>;

  return <div className="workspace">
    <SessionSidebar sessions={sessions} selected={selection} error={historyError} />
    <main className="workarea">
      <WorkspaceHeader codeMode={codeMode} quitting={quitting} onQuit={() => void quit()}
        session={sessions?.find(session => session.id === selection)} />
      {(quitError || (selection && catalogError)) && <div className="workspace-notice" role="alert">
        {quitError || catalogError}
      </div>}
      {selection ? <Conversation key={selection} id={selection} api={api} codeMode={codeMode} providers={catalog?.providers ?? []}
        onActivity={() => void refreshHistory()} onPromoted={id => {
          if (selectionFromUrl() === selection) window.location.hash = id;
          void refreshHistory();
        }} />
        : catalog ? <NewConversation api={api} catalog={catalog} onCreate={openCreated} />
        : <div className="content-scroll"><div className="measure">
          <h1>New conversation</h1>
          {catalogError ? <p className="error" role="alert">{catalogError}</p> : <p className="muted" role="status">Loading models…</p>}
        </div></div>}
    </main>
  </div>;
}
