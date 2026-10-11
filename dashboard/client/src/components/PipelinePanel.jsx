import React, { useEffect, useState } from 'react';
import { useFetch } from '../api.js';
import { currentRun, TYPE_INFO } from '../meta.js';

const STAGES = [
  ['plan', 'Plan', 'Planner agent ranks experiments'],
  ['inject', 'Inject', 'Guarded runs, adapter reacts'],
  ['analyze', 'Analyze', 'Analyst agent ranks + scores'],
  ['report', 'Report', 'Findings report written'],
];

function stageOf(job, lines) {
  if (!job) return -1;
  if (job.status !== 'running') return 4;
  if (job.meta?.type === 'plan') return 0;
  const text = lines.join('\n');
  if (/next: `chaos-agent report`/.test(text)) return 3;
  if (/analysing \d+ runs/.test(text)) return 2;
  if (/saved .*campaign\.json|Campaign \d{8}-\d{6}-campaign/.test(text)) return 2;
  if (job.runIds?.length || /baseline|campaign/i.test(text)) return 1;
  return 0;
}

export default function PipelinePanel({ jobState, goAgents }) {
  const { job, lines } = jobState;
  const running = job?.status === 'running';
  const [n, setN] = useState(3);
  const [noAdapt, setNoAdapt] = useState(false);
  const [plans, , reloadPlans] = useFetch('/plans');
  const latest = plans?.[0]?.id;
  const [plan, , reloadPlan] = useFetch(latest ? `/plans/${latest}` : null);

  useEffect(() => { if (job && !running) { reloadPlans(); } }, [job?.status]);   // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { reloadPlan(); }, [latest]);                                  // eslint-disable-line react-hooks/exhaustive-deps

  const stage = stageOf(job, lines);
  const run = currentRun(lines);
  const total = job?.meta?.count ?? plan?.experiments?.length ?? n;
  const doneRuns = job?.runIds ? job.runIds.length - (run && !run.done ? 1 : 0) : 0;

  return (
    <div className="card pipeline">
      <div className="card-head">
        <h2>AI chaos pipeline</h2>
        {job && <span className={`pill ${job.status}`}>{job.status}</span>}
      </div>
      <p className="story">The AI decides what to break. You approve, the agents plan, inject under the safety guard, and score the result.</p>

      <ol className="stages">
        {STAGES.map(([id, label, sub], i) => (
          <li key={id} className={i < stage ? 'done' : i === stage ? 'now' : ''}>
            <b>{i + 1}. {label}</b><small>{sub}</small>
          </li>
        ))}
      </ol>

      {running && job.meta?.type !== 'plan' && (
        <div className="now-running">
          {run && !run.done
            ? <>Failure {Math.min(job.runIds.length, job.meta?.count ?? total)} of {job.meta?.count ?? total}: <b>{TYPE_INFO[run.type]?.label ?? run.type}</b> on <b>{run.target}</b></>
            : stage >= 2 ? 'The analyst agent is scoring the results...' : 'Preparing the next experiment...'}
          <div className="bar"><i style={{ width: `${Math.min(100, (doneRuns / Math.max(1, total)) * 100)}%` }} /></div>
        </div>
      )}

      <div className="form inline">
        <label>Failures the AI injects, back to back
          <select value={n} disabled={running} onChange={(e) => setN(Number(e.target.value))}>
            <option value={2}>2 failures</option><option value={3}>3 failures</option><option value={4}>4 failures</option>
          </select>
        </label>
        <label className="check"><input type="checkbox" checked={noAdapt} disabled={running} onChange={(e) => setNoAdapt(e.target.checked)} /> skip adapter</label>
      </div>

      <div className="btn-col">
        <button className="btn primary big" disabled={running} onClick={() => jobState.start('/plan', { count: Number(n) })}>
          1 · Let the AI plan the experiments
        </button>
        <button className="btn danger big" disabled={running || !latest}
          onClick={() => jobState.start('/campaign', { planId: latest, count: Number(n), noAdapt })}>
          2 · Approve plan &amp; run the campaign
        </button>
        <button className="btn ghost" disabled={running} onClick={() => jobState.start('/campaign', { count: Number(n), noAdapt })}>
          Autopilot: plan + inject + analyze + report
        </button>
        {running && <button className="btn kill" onClick={jobState.abort}>■ Abort now</button>}
      </div>
      {jobState.error && <div className="alert">{jobState.error}</div>}

      <div className="plan-link">
        {latest ? <>Plan ready: <b>{plan?.experiments?.length ?? '?'} failures</b> ({plan?.source === 'ai' ? 'AI' : 'fallback'}). </> : 'No plan yet. '}
        <button className="btn ghost small" onClick={goAgents}>See the planner's ranked plan in AI Agents →</button>
      </div>
      <p className="fineprint">Every experiment is checked against hard caps, dry-run by Kubernetes, watched by the deterministic guard each second, and reverts by itself. The AI never runs kubectl.</p>
    </div>
  );
}
