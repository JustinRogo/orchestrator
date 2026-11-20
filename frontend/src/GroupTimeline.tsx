import { isRunActive, newRequestId, type GroupReply, type GroupRun, type GroupSession, type GroupView, type Provider,
  type RunAction, type RunStage, type RunStatus, type Synthesis } from './api';
import { ConversationPane, MessageCard, providerName } from './Workspace';

const stageNames: Record<RunStage['kind'], string> = {
  answer: 'Independent answers', review: 'Peer review', synthesis: 'Synthesis', expand: 'Follow-up',
  roundtable: 'Discussion turn', relay: 'Relay step', argument: 'Opening arguments', rebuttal: 'Rebuttal round',
  judgment: 'Judge’s assessment',
};

const statuses: Record<RunStatus, string> = {
  ready: 'Ready for the next stage', running: 'Running', pausing: 'Pausing after active calls', stopping: 'Stopping after active calls',
  paused: 'Paused', complete: 'Finished', stopped: 'Stopped', limited: 'Limit reached', interrupted: 'Interrupted', cancelled: 'Cancelled',
};

export interface Confirmation {
  title: string;
  description: string;
}

type OnAction = (run: GroupRun, action: RunAction, confirmation?: Confirmation) => void;

function SynthesisResult({ values }: { values: Synthesis }) {
  const lists = [
    ['Agreement', values.agreement], ['Disagreement', values.disagreement],
    ['Uncertainties', values.uncertainties], ['Next steps', values.next_steps],
  ] as const;

  return <dl className="synthesis">
    <div><dt>Recommendation</dt><dd className="message-text">{values.recommendation}</dd></div>
    {lists.map(([label, items]) => <div key={label}>
      <dt>{label}</dt>
      <dd>{items.length > 0
        ? <ul className="message-text">{items.map((item, index) => <li key={index}>{item}</li>)}</ul>
        : <p className="muted">None reported.</p>}
      </dd>
    </div>)}
  </dl>;
}

function ReplyBody({ reply, draft }: { reply: GroupReply; draft?: string }) {
  const pending = reply.status === 'pending';
  const used = pending ? 'In progress' : reply.total_tokens == null ? 'Tokens unavailable' : `${reply.total_tokens.toLocaleString()} tokens`;
  const source = reply.input_reply_ids.length
    ? `From ${reply.input_reply_ids.map(id => `#${id}`).join(', ')}` : 'Shared prompt and context';
  const details = [`Reply #${reply.id}`, source, used];
  if (reply.finished_at) details.push(new Date(reply.finished_at).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }));
  if (reply.imported) details.push('Imported from Compare');
  if (reply.context_truncated || reply.output_limited) details.push('Text shortened');

  return <>
    {reply.error && <p className="error">{reply.error}</p>}
    {reply.synthesis ? <SynthesisResult values={reply.synthesis} />
      : (reply.response || pending) && <p className="message-text">{reply.response ?? draft ?? 'Waiting for this model…'}</p>}
    <p className="muted metadata">{details.join(' · ')}</p>
  </>;
}

function ReplyCard({ session, run, stage, participant, providers, drafts, canAdvance, busy, onAction }: {
  session: GroupSession;
  run: GroupRun;
  stage: RunStage;
  participant: number;
  providers: Provider[];
  drafts: Record<string, string>;
  canAdvance: boolean;
  busy: boolean;
  onAction: OnAction;
}) {
  const model = session.strategy_options.participants[participant];
  const attempts = stage.replies.filter(reply => reply.participant === participant);
  const reply = attempts.at(-1);
  const author = `${providerName(providers, model.provider)} · ${reply?.response_model ?? model.model}`;
  const chosen = Boolean(reply && run.selected_reply === reply.id);
  const skipped = stage.done || ['stopped', 'limited', 'cancelled'].includes(run.status);
  const answered = reply?.status === 'complete' && stage.kind !== 'synthesis';
  const retry = reply && canAdvance && run.controls.retry.includes(reply.id);
  const choose = answered && session.strategy === 'compare' && !run.inherited;
  const expand = answered && canAdvance && run.controls.expand;

  return <article className={`reply-card${chosen ? ' chosen' : ''}`} data-pending={reply?.status === 'pending' || undefined}>
    <h4>{author}</h4>
    {model.role && <p className="muted participant-role">{stage.kind === 'judgment' ? 'Judge' : model.role}</p>}
    {reply ? <>
      <ReplyBody reply={reply} draft={drafts[`run:${run.id}:reply:${reply.id}`]} />
      {attempts.length > 1 && <details className="settings">
        <summary>Earlier attempts</summary>
        {attempts.slice(0, -1).map(attempt => <div key={attempt.id} className="earlier-attempt"><ReplyBody reply={attempt} /></div>)}
      </details>}
      {(retry || choose || expand) && <div className="reply-controls">
          {retry && <button type="button" disabled={busy}
            onClick={() => onAction(run, { kind: 'retry', reply_id: reply.id })}>Retry reply</button>}
          {choose && <button type="button" disabled={busy} aria-pressed={chosen}
            onClick={() => onAction(run, { kind: 'choose', reply_id: reply.id })}>{chosen ? 'Chosen reply' : 'Choose reply'}</button>}
          {expand && <button type="button" disabled={busy}
            onClick={() => onAction(run, { kind: 'expand', reply_id: reply.id, instruction: '' }, {
              title: 'Ask follow-up', description: `Only ${author} receives this question and its earlier reply.`,
            })}>Ask follow-up</button>}
        </div>}
    </> : <p className="muted">{skipped ? 'Skipped' : 'Waiting to run'}</p>}
  </article>;
}

function RunControls({ run, strategy, canAdvance, busy, onAction }: {
  run: GroupRun;
  strategy: GroupSession['strategy'];
  canAdvance: boolean;
  busy: boolean;
  onAction: OnAction;
}) {
  const { controls } = run;
  const active = isRunActive(run);
  const last = run.stages.at(-1);
  if (!controls.pause && !controls.stop && !controls.cancel && !controls.promote
    && !(canAdvance && (controls.next || controls.synthesize))) return null;

  return <div className={`run-controls${active ? ' active' : ''}`}>
    {active && last && <span className="muted">{stageNames[last.kind]}</span>}
    {controls.pause && <button type="button" disabled={busy} onClick={() => onAction(run, { kind: 'pause' })}>Pause after active calls</button>}
    {controls.stop && <button type="button" disabled={busy} onClick={() => onAction(run, { kind: 'stop' })}>
      {active ? 'Stop after active calls' : 'Stop run'}
    </button>}
    {controls.cancel && <button className="danger" type="button" disabled={busy}
      title="Cancel local requests. Providers may still charge for work they received."
      onClick={() => onAction(run, { kind: 'cancel' })}>Cancel run</button>}
    {canAdvance && controls.next && <>
      <button type="button" disabled={busy} onClick={() => onAction(run, { kind: 'next' })}>
        {last?.done ? strategy === 'roundtable' ? 'Next turn' : 'Next stage' : 'Continue stage'}
      </button>
      {strategy === 'roundtable' && last?.done && <button type="button" disabled={busy}
        onClick={() => onAction(run, { kind: 'next' }, {
          title: 'Direct the next turn', description: 'Choose a speaker and add an optional direction for this turn.',
        })}>Direct next turn</button>}
      {strategy !== 'compare' && <button type="button" disabled={busy}
        onClick={() => onAction(run, { kind: 'next', auto_advance: true })}>Advance automatically</button>}
    </>}
    {canAdvance && controls.synthesize && <button type="button" disabled={busy}
      onClick={() => onAction(run, { kind: 'synthesize', share_history: strategy === 'compare' }, strategy === 'compare' ? {
        title: 'Synthesize now', description: 'Send the available replies to the selected synthesis model.',
      } : undefined)}>Synthesize now</button>}
    {controls.promote && <button type="button" disabled={busy}
      onClick={() => onAction(run, { kind: 'promote', share_history: true, request_id: newRequestId() }, {
        title: 'Send to AI Council', description: 'Copy these independent answers into a new Council. Peer review and synthesis can then share them across the selected providers.',
      })}>Send to AI Council</button>}
  </div>;
}

function RunCard({ session, run, index, providers, drafts, canAdvance, busy, onAction }: {
  session: GroupSession;
  run: GroupRun;
  index: number;
  providers: Provider[];
  drafts: Record<string, string>;
  canAdvance: boolean;
  busy: boolean;
  onAction: OnAction;
}) {
  const { budget } = run;
  const usage = isRunActive(run) ? 'Usage pending'
    : budget.used.tokens === null ? 'Token total unavailable' : `${budget.used.tokens.toLocaleString()} tokens`;
  const cap = budget.limit.tokens === null ? '' : ` / ${budget.limit.tokens.toLocaleString()} token budget`;
  const controls = <RunControls run={run} strategy={session.strategy} busy={busy} canAdvance={canAdvance} onAction={onAction} />;
  const controlsAtEnd = ['roundtable', 'relay', 'debate'].includes(session.strategy) && !isRunActive(run);

  return <section className="run" aria-labelledby={`run-${run.id}`}>
    <h2 id={`run-${run.id}`}>{`Run ${index + 1}`}</h2>
    <MessageCard variant="user" author="You" content={run.prompt} heading="h3" />
    {run.context && <details className="settings">
      <summary>Shared context</summary>
      <p className="message-text">{run.context}</p>
    </details>}
    {run.source && <a className="source-link" href={`#${run.source.session_id}`}>View source Compare conversation</a>}
    <p className="muted run-status" role="status">
      {run.inherited ? 'Copied from source' : statuses[run.status]}{run.incomplete && ' · Incomplete results'} · {budget.calls}/{budget.max_calls} model calls · {usage}{cap}
    </p>
    {run.note && <p className="error">{run.note}</p>}
    {!controlsAtEnd && controls}
    {run.stages.map((stage, stageIndex) => <section key={stage.id} className="stage">
      <h3>{stageNames[stage.kind]}{['review', 'roundtable', 'relay', 'rebuttal'].includes(stage.kind)
        && ` ${run.stages.slice(0, stageIndex + 1).filter(item => item.kind === stage.kind).length}`}</h3>
      {stage.instruction && <MessageCard variant="user" author="You" content={stage.instruction} heading="h4" />}
      <div className="reply-grid">
        {stage.targets.map(participant => <ReplyCard key={participant} session={session} run={run} stage={stage}
          participant={participant} providers={providers} drafts={drafts} canAdvance={canAdvance} busy={busy} onAction={onAction} />)}
      </div>
    </section>)}
    {controlsAtEnd && controls}
  </section>;
}

export function GroupTimeline({ view, providers, drafts, busy, onAction }: {
  view: GroupView;
  providers: Provider[];
  drafts: Record<string, string>;
  busy: boolean;
  onAction: OnAction;
}) {
  const { session, runs } = view;
  const settings = session.strategy_options;
  const synth = session.strategy === 'compare' || session.strategy === 'council'
    ? settings.participants[settings.synthesizer] : null;
  const judge = settings.judge === undefined ? null : settings.participants[settings.judge];
  const active = runs.some(isRunActive);

  return <ConversationPane title={session.title} follow={active} wide>
    <details className="settings conversation-settings">
      <summary>Conversation settings</summary>
      <p className="muted">{session.max_turns} prompts · {session.max_output_tokens} output tokens per reply · {session.timeout_seconds}s timeout</p>
      <p className="muted">{settings.max_invocations} model calls per run{settings.token_budget === null ? '' : ` · ${settings.token_budget.toLocaleString()} token budget per run`}
        {settings.token_budget === null && settings.parallel !== false && ' · Parallel independent replies'}</p>
      {synth && <p className="muted">Synthesis by {providerName(providers, synth.provider)} · {synth.model}</p>}
      {judge && <p className="muted">Judged by {providerName(providers, judge.provider)} · {judge.model} · {settings.rebuttal_rounds} rebuttal rounds</p>}
      {settings.roundtable_turns && <p className="muted">{settings.roundtable_turns} discussion turns per run</p>}
      {session.strategy === 'relay' && <p className="muted">Relay steps · {settings.participants.map(model => model.role || model.model).join(' → ')}</p>}
      {session.strategy === 'council' && <p className="muted">{settings.review_rounds} planned peer review {settings.review_rounds === 1 ? 'round' : 'rounds'}</p>}
      {session.system_prompt && <p className="message-text">{session.system_prompt}</p>}
    </details>
    {runs.length === 0 && <p className="muted">Enter a shared prompt to start a run.</p>}
    {runs.map((run, index) => <RunCard key={run.id} run={run} session={session} index={index} providers={providers}
      drafts={drafts} canAdvance={!active} busy={busy} onAction={onAction} />)}
  </ConversationPane>;
}
