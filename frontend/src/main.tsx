import { createRoot } from 'react-dom/client';
import { createApi } from './api';
import { App } from './App';
import { CodeWorkspace } from './CodeWorkspace';
import './styles.css';

const host = document.getElementById('root');
if (!host) throw new Error('The AI Team dashboard is missing its root element.');

const token = document.querySelector<HTMLMetaElement>('meta[name="ai-team-token"]')?.content;
const codeMode = document.querySelector<HTMLMetaElement>('meta[name="ai-team-code-mode"]')?.content === 'enabled';
const defaultMode = document.querySelector<HTMLMetaElement>('meta[name="ai-team-default-mode"]')?.content;
const root = createRoot(host);

if (token && token !== '__AI_TEAM_TOKEN__') {
  const api = createApi(token);
  root.render(codeMode && defaultMode === 'code' ? <CodeWorkspace api={api} /> : <App api={api} codeMode={codeMode} />);
} else {
  root.render(<main className="stopped"><h1>Open the dashboard through AI Team</h1>
    <p>Launch the Python server with <code>ai-team ui</code>.</p></main>);
}
