import type { Agent, Recipient } from './codeTypes';

const names: Record<Agent | 'human' | 'system' | 'all', string> = {
  codex: 'Codex', claude: 'Claude', gemini: 'Antigravity', human: 'You', system: 'System', all: 'Everyone',
};

export const agentName = (name: string) => (names as Record<string, string>)[name] ?? name;
export const taskState = (status: string, active: boolean) => active ? 'Running' : status[0].toUpperCase() + status.slice(1).replaceAll('_', ' ');

export function parseRecipient(prompt: string, fallback: Recipient): { recipient: Recipient; message: string } {
  const trimmed = prompt.trim();
  const prefix = trimmed.match(/^@(everyone|all|codex|claude|gemini|antigravity)(?=$|[\s,:-])/i);
  if (!prefix) {
    if (trimmed.startsWith('@')) throw new Error('Choose a recipient from the @ list.');
    return { recipient: fallback, message: trimmed };
  }
  const name = prefix[1].toLowerCase();
  const recipient = name === 'everyone' ? 'all' : name === 'antigravity' ? 'gemini' : name as Recipient;
  return { recipient, message: trimmed.slice(prefix[0].length).replace(/^[\s,:-]+/, '').trim() };
}

export function displayedMessage(content: string): string {
  const trimmed = content.trim();
  if (!trimmed.startsWith('```json') || !trimmed.endsWith('```')) return content;
  try {
    const parsed = JSON.parse(trimmed.slice(7, -3).trim());
    if (typeof parsed?.message !== 'string') return content;
    return parsed.delegate_to && parsed.delegated_task
      ? `${parsed.message}\n\nRequest to ${parsed.delegate_to}: ${parsed.delegated_task}` : parsed.message;
  } catch {
    return content;
  }
}
