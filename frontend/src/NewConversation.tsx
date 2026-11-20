import { useEffect, useState, type FormEvent } from 'react';
import { errorText, type Api, type GroupStrategy, type ModelCatalog, type ModelChoice, type NewGroupSession,
  type Participant, type SavedTeam, type Session, type Strategy } from './api';
import { availableModels, modelKey, ModelPicker, providerName, strategyNames } from './Workspace';

const modeDescription: Record<Strategy, string> = {
  single: 'Talk with one model and switch models between replies.',
  compare: 'Send the same prompt to two or three models and read their independent replies side by side.',
  council: 'Collect independent answers, run peer review, and synthesize the points of agreement and disagreement.',
  roundtable: 'Take turns in a shared discussion. Choose who speaks next and add direction between turns.',
  relay: 'Hand work from one model to the next. Each model takes a specific step.',
  debate: 'Assign viewpoints, hear opening arguments and rebuttals, then have a separate model evaluate the evidence.',
};

const stepRoles = ['Draft', 'Critique', 'Validate', 'Revise'];
const viewpoints = ['Advocate for the proposal', 'Challenge the proposal', 'Offer an alternative viewpoint'];
const pickLabels = ['First model', 'Second model', 'Third model', 'Fourth model'];

function orderTeams(teams: SavedTeam[]) {
  return [...new Map(teams.map(team => [team.id, team])).values()].sort((a, b) => a.name.localeCompare(b.name));
}

export function NewConversation({ api, catalog, onCreate }: {
  api: Api;
  catalog: ModelCatalog;
  onCreate: (session: Session) => void;
}) {
  const [model, setModel] = useState<ModelChoice | null>(() => availableModels(catalog.providers)[0] ?? null);
  const [strategy, setStrategy] = useState<Strategy>('single');
  const [picks, setPicks] = useState<(Participant | null)[]>(() => {
    const options = availableModels(catalog.providers);
    return [options[0] ?? null, options[1] ?? null, null, null];
  });
  const [synthesizer, setSynthesizer] = useState('');
  const [judge, setJudge] = useState('');
  const [teams, setTeams] = useState<SavedTeam[]>([]);
  const [teamName, setTeamName] = useState('');
  const [teamStatus, setTeamStatus] = useState<{ action: 'loaded' | 'saved'; name: string }>();
  const [teamError, setTeamError] = useState('');
  const [template, setTemplate] = useState<{ team: SavedTeam; revision: number }>();
  const [creating, setCreating] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const { defaults, providers } = catalog;
  const available = availableModels(providers);
  const missing = providers.filter(provider => !provider.configured && provider.models.length > 0);
  const group = strategy !== 'single';
  const preset = ['roundtable', 'relay', 'debate'].includes(strategy);
  const sequential = strategy === 'roundtable' || strategy === 'relay';
  const minPicks = strategy === 'debate' ? 3 : 2;
  const visiblePicks = picks.slice(0, preset ? 4 : 3);
  const participants = visiblePicks.filter(pick => pick !== null);
  const uniqueParticipants = [...new Map(participants.map(pick => [modelKey(pick), pick])).values()];
  const choices = uniqueParticipants.map(modelKey);
  const synthesisModel = choices.includes(synthesizer) ? synthesizer : choices[0] ?? '';
  const judgeModel = choices.includes(judge) ? judge : choices.at(-1) ?? '';
  const judgeSlot = visiblePicks.findIndex(pick => pick && modelKey(pick) === judgeModel);
  const allConfigured = participants.every(pick => available.some(option => modelKey(pick) === modelKey(option)));
  const groupReady = visiblePicks.slice(0, minPicks).every(Boolean) && participants.length === uniqueParticipants.length && allConfigured;
  const valid = group ? groupReady : Boolean(model);
  const busy = creating || saving;
  const saved = template?.team;
  const savedGroup = saved?.strategy_options;

  useEffect(() => {
    const controller = new AbortController();
    void api.listTeams(controller.signal).then(({ teams }) => setTeams(current => orderTeams([...teams, ...current]))).catch(error => {
      if (!controller.signal.aborted) setTeamError(errorText(error));
    });
    return () => controller.abort();
  }, [api]);

  function selectStrategy(next: Strategy) {
    setStrategy(next);
    setError('');
    if (next === 'debate' && !picks[2]) {
      const third = available.find(option => !picks.some(pick => pick && modelKey(pick) === modelKey(option)));
      if (third) updatePick(2, third);
    }
  }

  function updatePick(index: number, next: Participant | null) {
    setPicks(current => current.map((value, slot) => slot === index ? next : value));
  }

  function loadTeam(id: string) {
    const team = teams.find(item => item.id === id);
    if (!team) return;
    const { participants, synthesizer, judge } = team.strategy_options;
    setStrategy(team.strategy);
    setPicks(pickLabels.map((_, index) => participants[index] ?? null));
    setSynthesizer(modelKey(participants[synthesizer]));
    setJudge(judge === undefined ? '' : modelKey(participants[judge]));
    setTemplate(current => ({ team, revision: (current?.revision ?? 0) + 1 }));
    setTeamError('');
    setError('');
    setTeamStatus({ action: 'loaded', name: team.name });
  }

  function readSettings(fields: FormData) {
    return {
      system_prompt: String(fields.get('system_prompt') ?? ''),
      max_turns: Number(fields.get('max_turns')),
      max_output_tokens: Number(fields.get('max_output_tokens')),
      timeout_seconds: Number(fields.get('timeout_seconds')),
    };
  }

  function readGroup(fields: FormData, strategy: GroupStrategy): NewGroupSession {
    return {
      ...readSettings(fields),
      strategy,
      participants,
      synthesizer: participants.findIndex(pick => modelKey(pick) === synthesisModel),
      max_invocations: Number(fields.get('max_invocations')),
      token_budget: Number(fields.get('token_budget')) || null,
      review_rounds: strategy === 'council' ? Number(fields.get('review_rounds')) : 0,
      auto_advance: fields.has('auto_advance'),
      parallel: fields.has('parallel'),
      ...(strategy === 'roundtable' && { roundtable_turns: Number(fields.get('roundtable_turns')) || participants.length }),
      ...(strategy === 'debate' && { judge: participants.findIndex(pick => modelKey(pick) === judgeModel),
        rebuttal_rounds: Number(fields.get('rebuttal_rounds')) }),
    };
  }

  async function save(form: HTMLFormElement | null) {
    if (strategy === 'single' || !teamName.trim() || !form?.reportValidity() || !valid || busy) return;
    setSaving(true);
    setTeamError('');
    setTeamStatus(undefined);
    try {
      const { team } = await api.saveTeam({ name: teamName, ...readGroup(new FormData(form), strategy) });
      setTeams(current => orderTeams([...current, team]));
      setTeamName('');
      setTeamStatus({ action: 'saved', name: team.name });
    } catch (error) {
      setTeamError(errorText(error));
    } finally {
      setSaving(false);
    }
  }

  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!valid || busy) return;
    const fields = new FormData(event.currentTarget);
    const body = strategy === 'single' ? model && { ...readSettings(fields), strategy, ...model } : readGroup(fields, strategy);
    if (!body) return;
    setCreating(true);
    setError('');
    try {
      const { session } = await api.createSession(body);
      onCreate(session);
    } catch (error) {
      setError(errorText(error));
    } finally {
      setCreating(false);
    }
  }

  return <div className="content-scroll new-conversation">
    <div className="measure">
      <h1>New conversation</h1>
      <form onSubmit={create} onInvalid={event => {
        const details = (event.target as HTMLElement).closest('details');
        if (details) details.open = true;
      }}>
        {teams.length > 0 && <div className="field">
          <label htmlFor="saved-team">Use a saved team</label>
          <select id="saved-team" value="" disabled={busy} onChange={event => loadTeam(event.target.value)}>
            <option value="">Choose a team</option>
            {teams.map(team => <option key={team.id} value={team.id}>{team.name} · {strategyNames[team.strategy]}</option>)}
          </select>
        </div>}
        {teamStatus?.action === 'loaded' && <p className="muted" role="status">Loaded {teamStatus.name}.</p>}
        <div className="field">
          <label htmlFor="strategy">Mode</label>
          <select id="strategy" value={strategy} disabled={busy} onChange={event => selectStrategy(event.target.value as Strategy)}>
            {Object.entries(strategyNames).map(([value, name]) => <option key={value} value={value}>{name}</option>)}
          </select>
        </div>
        <p className="muted">{modeDescription[strategy]}</p>
        {group ? <>
          <div className="participant-grid">
            {visiblePicks.map((pick, index) => {
              const previous = picks.slice(0, index).filter(Boolean).length;
              const defaultRole = strategy === 'relay' ? stepRoles[previous] : strategy === 'debate'
                ? index === judgeSlot ? 'Judge' : viewpoints[previous - Number(judgeSlot >= 0 && judgeSlot < index)] : '';
              const label = pickLabels[index];
              return <div className="participant-slot" key={index}>
                <ModelPicker id={`participant-${index + 1}`} label={label + (index >= minPicks ? ' (optional)' : '')}
                  selected={pick} onSelect={next => updatePick(index, next && { ...pick, ...next })}
                  providers={providers} optional={index >= minPicks} disabled={busy} />
                {pick && <details className="settings">
                  <summary>{pick.role || defaultRole ? `Role · ${pick.role || defaultRole}` : 'Role and instructions'}</summary>
                  <div className="field">
                    <label htmlFor={`role-${index}`}>{strategy === 'debate' ? 'Viewpoint or role' : 'Role'}</label>
                    <input id={`role-${index}`} value={pick.role ?? ''} maxLength={160} placeholder={defaultRole || 'Optional'}
                      onChange={event => updatePick(index, { ...pick, role: event.target.value })} disabled={busy} />
                  </div>
                  <div className="field">
                    <label htmlFor={`instructions-${index}`}>Instructions</label>
                    <textarea id={`instructions-${index}`} value={pick.instructions ?? ''} rows={2}
                      maxLength={2000} onChange={event => updatePick(index, { ...pick, instructions: event.target.value })} disabled={busy} />
                  </div>
                </details>}
              </div>;
            })}
          </div>
          {!sequential && <div className="field">
            <label htmlFor="synthesizer">{strategy === 'debate' ? 'Independent judge' : 'Synthesis model'}</label>
            <select id="synthesizer" disabled={busy || uniqueParticipants.length === 0}
              value={strategy === 'debate' ? judgeModel : synthesisModel}
              onChange={event => strategy === 'debate' ? setJudge(event.target.value) : setSynthesizer(event.target.value)}>
              {!uniqueParticipants.length && <option value="">Choose participating models</option>}
              {uniqueParticipants.map(pick => <option key={modelKey(pick)} value={modelKey(pick)}>
                {providerName(providers, pick.provider)} · {pick.model}
              </option>)}
            </select>
          </div>}
          {!groupReady && <p className="muted" role="status">Choose {minPicks} to {visiblePicks.length} distinct models with configured API keys.</p>}
        </> : <ModelPicker id="initial-model" label="Model" selected={model} onSelect={setModel} providers={providers} disabled={busy} />}
        {missing.length > 0 && <details className="settings" open={model ? undefined : true}>
          <summary>API credentials</summary>
          <p className="muted">Set the environment variables for the providers you want to use, then relaunch AI Team.</p>
          <ul className="credentials">
            {missing.map(provider => <li key={provider.id}>{provider.name} <code>{provider.credential_variable}</code></li>)}
          </ul>
        </details>}
        <details className="settings" key={template?.revision}>
          <summary>Conversation settings</summary>
          <div className="field">
            <label htmlFor="system-prompt">System instructions</label>
            <textarea id="system-prompt" name="system_prompt" rows={3} maxLength={4000} defaultValue={saved?.system_prompt} />
          </div>
          <div className="limits">
            <div className="field">
              <label htmlFor="max-turns">Maximum prompts</label>
              <input id="max-turns" name="max_turns" type="number" min={1} max={100} defaultValue={saved?.max_turns ?? defaults.max_turns} required />
            </div>
            <div className="field">
              <label htmlFor="max-tokens">Output tokens per reply</label>
              <input id="max-tokens" name="max_output_tokens" type="number" min={256} max={16384} defaultValue={saved?.max_output_tokens ?? defaults.max_output_tokens} required />
            </div>
            <div className="field">
              <label htmlFor="timeout">Timeout in seconds</label>
              <input id="timeout" name="timeout_seconds" type="number" min={5} max={180} defaultValue={saved?.timeout_seconds ?? defaults.timeout_seconds} required />
            </div>
          </div>
          {group && <>
            <div className="limits">
              <div className="field">
                <label htmlFor="max-calls">Model calls per run</label>
                <input id="max-calls" name="max_invocations" type="number" min={Math.max(2, participants.length)} max={32} defaultValue={savedGroup?.max_invocations ?? 16} required />
              </div>
              <div className="field">
                <label htmlFor="token-budget">Token budget per run</label>
                <input id="token-budget" name="token_budget" type="number" min={1} max={10000000} placeholder="Optional" defaultValue={savedGroup?.token_budget ?? undefined} />
              </div>
              {strategy === 'council' && <div className="field">
                <label htmlFor="review-rounds">Peer review rounds</label>
                <input id="review-rounds" name="review_rounds" type="number" min={0} max={3} defaultValue={saved?.strategy === 'council' ? savedGroup?.review_rounds : 1} required />
              </div>}
              {strategy === 'roundtable' && <div className="field" key="roundtable-turns">
                <label htmlFor="roundtable-turns">Discussion turns per run</label>
                <input id="roundtable-turns" name="roundtable_turns" type="number" min={1} max={16}
                  defaultValue={savedGroup?.roundtable_turns} placeholder={String(participants.length)} />
              </div>}
              {strategy === 'debate' && <div className="field" key="rebuttal-rounds">
                <label htmlFor="rebuttal-rounds">Rebuttal rounds</label>
                <input id="rebuttal-rounds" name="rebuttal_rounds" type="number" min={1} max={3}
                  defaultValue={savedGroup?.rebuttal_rounds ?? 1} required />
              </div>}
            </div>
            <div className="setting-checks">
              {strategy !== 'compare' && <label><input name="auto_advance" type="checkbox"
                defaultChecked={savedGroup?.auto_advance} /> Advance automatically after each successful stage</label>}
              {!sequential && <label><input name="parallel" type="checkbox" defaultChecked={savedGroup?.parallel ?? true} />
                Run independent replies in parallel when the token budget allows</label>}
            </div>
          </>}
        </details>
        {group && <details className="settings">
          <summary>Save as a reusable team</summary>
          <div className="field">
            <label htmlFor="team-name">Team name</label>
            <input id="team-name" value={teamName} onChange={event => setTeamName(event.target.value)} disabled={busy}
              maxLength={120} onKeyDown={event => {
                if (event.key === 'Enter') {
                  event.preventDefault();
                  void save(event.currentTarget.form);
                }
              }} />
          </div>
          <button type="button" disabled={!valid || busy || !teamName.trim()}
            onClick={event => void save(event.currentTarget.form)}>{saving ? 'Saving…' : 'Save team'}</button>
          {teamStatus?.action === 'saved' && <p className="muted metadata" role="status">Saved {teamStatus.name}.</p>}
        </details>}
        {teamError && <p className="error" role="alert">{teamError}</p>}
        {error && <p className="error" role="alert">{error}</p>}
        <div className="actions">
          <button className="primary" type="submit" disabled={!valid || busy}>
            {creating ? 'Starting…' : 'Start conversation'}
          </button>
        </div>
      </form>
    </div>
  </div>;
}
