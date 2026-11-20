import { useState } from 'react';
import { agentNames, type Agent, type CodeOverview, type CodeTask, type TaskChanges } from './codeTypes';
import { agentName, taskState } from './codeText';

export function CodeInspector({ task, overview, changes, diffError }: {
  task?: CodeTask;
  overview?: CodeOverview;
  changes?: TaskChanges;
  diffError: string;
}) {
  const [agent, setAgent] = useState<Agent>('codex');
  const active = changes?.[agent];
  const queue = task?.activity?.agent === task?.queue[0]?.agent ? task?.queue.slice(1) : task?.queue;
  const tests = task?.artifacts.flatMap(artifact => artifact.tests ?? []).slice(-5) ?? [];
  const budget = task?.budget_summary;
  const budgetKeys = ['tokens', 'cost_usd'] as const;

  function amount(value: number | null, unit: string) {
    return value === null ? 'unknown' : `${unit}${value.toLocaleString(undefined, { maximumFractionDigits: 6 })}`;
  }

  return <aside className="code-inspector" aria-labelledby="inspector-title" id="task-details" tabIndex={-1}>
    <h2 id="inspector-title">Task details</h2>
    <section className="inspector-section">
      <h3>Overview</h3>
      {task ? <dl className="inspector-list">
        <div><dt>Task ID</dt><dd>{task.id}</dd></div>
        <div><dt>Status</dt><dd>{taskState(task.status, task.active)}</dd></div>
        <div><dt>Agent turns</dt><dd>{task.turn_count}</dd></div>
        <div><dt>Round</dt><dd>{task.current_round} / {task.max_rounds}</dd></div>
        {budget && budgetKeys.map(key => {
          if (budget.limit[key] === null) return null;
          const unit = key === 'cost_usd' ? '$' : '';
          return <div key={key}><dt>{key === 'tokens' ? 'Token budget' : 'Cost budget'}</dt>
            <dd>Used {amount(budget.used[key], unit)} · Remaining {amount(budget.remaining[key], unit)} of {amount(budget.limit[key], unit)}</dd></div>;
        })}
        {task.pause_reason && <div><dt>Pause reason</dt><dd>{task.pause_reason}</dd></div>}
        {task.activity?.agent && <div><dt>Current activity</dt><dd>{agentName(task.activity.agent)} {task.activity.stage}</dd></div>}
        {agentNames.map(name => task.roles[name] && <div key={name}><dt>{agentName(name)} role</dt>
          <dd>{overview?.role_options[task.roles[name] as keyof CodeOverview['role_options']] ?? task.roles[name]}</dd></div>)}
        <div><dt>Queued agents</dt><dd>{queue?.length ? queue.map(item => agentName(item.agent)).join(', ') : 'None'}</dd></div>
      </dl> : <p className="muted">Select a task to see its progress.</p>}
    </section>
    <section className="inspector-section">
      <h3>Changed files and Git diff</h3>
      {task && <div className="diff-agents" role="group" aria-label="Agent worktree">
        {agentNames.map(name => <button key={name} type="button" aria-pressed={agent === name}
          onClick={() => setAgent(name)}>{agentName(name)}</button>)}
      </div>}
      {diffError && <p className="error" role="alert">{diffError}</p>}
      {active?.files.length ? <ul className="changed-files" aria-label="Changed files">
        {active.files.map(file => <li key={file}>{file}</li>)}
      </ul> : <p className="muted">No changed files.</p>}
      {active?.diff ? <pre tabIndex={0} aria-label="Git diff">{active.diff}{active.truncated && '\n\n[Diff truncated]'}</pre>
        : <p className="muted">{task && !changes && !diffError ? 'Loading diff…' : active?.files.length
          ? 'Changed files include untracked files. Inspect them in the worktree.' : active ? 'No diff.' : task ? 'No worktree yet.' : 'No task selected.'}</p>}
    </section>
    <section className="inspector-section">
      <h3>Test results</h3>
      {tests.length ? <ul className="test-results">
        {tests.map((run, index) => <li key={index}>
          <code>{run.command?.join(' ') || 'Test'}</code>
          <span className={run.error || run.exit_code !== 0 ? 'error' : 'test-passed'}>{run.error ? 'Error' : run.exit_code === 0 ? 'Passed' : 'Failed'}</span>
        </li>)}
      </ul> : <p className="muted">No configured test runs yet.</p>}
    </section>
  </aside>;
}
