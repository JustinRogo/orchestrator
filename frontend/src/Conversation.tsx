import type { Api, Provider } from './api';
import { ChatConversation } from './ChatConversation';
import { GroupConversation } from './GroupConversation';
import { ConversationTools } from './ConversationTools';
import { useConversation } from './useConversation';

export function Conversation({ api, id, providers, codeMode, onActivity, onPromoted }: {
  api: Api;
  id: string;
  providers: Provider[];
  codeMode: boolean;
  onActivity: () => void;
  onPromoted: (id: string) => void;
}) {
  const { view, drafts, loadError, connectionStatus, reload } = useConversation(api, id);

  if (!view) return <div className="content-scroll"><div className="measure">
    <h1>Conversation</h1>
    {loadError ? <p className="error" role="alert">{loadError}</p> : <p className="muted" role="status">Loading conversation…</p>}
  </div></div>;

  const shared = { api, drafts, providers, connectionStatus, reload, onActivity };
  return <>
    <ConversationTools api={api} view={view} codeMode={codeMode} onBranch={onPromoted} />
    {'turns' in view ? <ChatConversation {...shared} view={view} />
      : <GroupConversation {...shared} view={view} onPromoted={onPromoted} />}
  </>;
}
