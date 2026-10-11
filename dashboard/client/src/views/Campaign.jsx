import React, { useEffect, useState } from 'react';
import { useFetch, ms, pct, shortId } from '../api.js';
import { TYPE_INFO } from '../meta.js';
import PlanView from '../components/PlanView.jsx';
import Terminal from '../components/Terminal.jsx';

const AGENTS = [
  { id: 'planner', title: 'Planner', when: 'Start of a campaign', does: 'Reads the dependency map and past runs, then ranks which experiments to run - each with a short reason, a hypothesis and a prediction.', tools: 'get_service · list_runs · get_run · check_experiment' },
  { id: 'adapter', title: 'Adapter', when: 'After every run', does: 'Compares prediction with what happened and decides: continue, skip, add a follow-up, or stop the campaign.', tools: 'get_run · get_timeline · check_experiment' },
  { id: 'analyst', title: 'Analyst', when: 'End of a campaign', does: 'Explains the findings, ranks weaknesses by severity with a concrete fix, and scores each service on a five-part rubric.', tools: 'get_run · get_timeline · get_service · get_fix_catalog' },
];

const verdictTone = { confirmed: 'ok', partly: 'warn', refuted: 'bad' };

export default function Campaign({ ctx, onOpenRun, onOpenReport, goLive }) {
  const { jobState, graph } = ctx;
  const { job, lines } = jobState;
  const running = job?.status === 'running';
  const [plans] = useFetch('/plans', [job?.status]);
  const [planSel, setPlanSel] = useState(null);
  const [plan] = useFetch(planSel ? `/plans/${planSel}` : null);
  const [list] = useFetch('/campaigns', [job?.status]);
  const [sel, setSel] = useState(null);
  const [data] = useFetch(sel ? `/campaigns/${sel}` : null, [job?.status]);

  useEffect(() => { if (!planSel && plans?.length) setPlanSel(plans[0].id); }, [plans, planSel]);
  useEffect(() => { if (!sel && list?.length) setSel(list[0].id); }, [list, sel]);

  const text = lines.join('\n');
  const stage = !running ? null : job?.meta?.type === 'plan' ? 'planner' : /analysing \d+ runs/.test(text) ? 'analyst'
    : /adapter|decision/i.test(text) && job?.runIds?.length ? 'adapter' : job?.runIds?.length ? 'adapter' : 'planner';

  const c = data?.campaign;
  return (
    <div className="view campaign">
      <div className="agents">
        {AGENTS.map((a, i) => (
          <div key={a.id} className={`card agent ${stage === a.id ? 'active' : ''}`}>
            <div className="agent-top"><span className="num">{i + 1}</span><h3>{a.title}</h3><span className="chip">{a.when}</span></div>
            <p>{a.does}</p>
            <code>{a.tools}</code>
          </div>
        ))}
        <div className="card rules">
          <h3>Guard rails</h3>
          <ul>
            <li>The AI chooses the experiments - nobody injects faults by hand.</li>
            <li>The AI never runs kubectl or shell.</li>
            <li>Every proposal passes hard caps + a server-side dry-run.</li>
            <li>The safety guard is deterministic code; AI can only tighten it.</li>
            <li>No API? A labelled fallback planner takes over.</li>
          </ul>
        </div>
      </div>

      {running && (
        <div className="card">
          <div className="card-head"><h2>Live agent output</h2><span className="pill running">{job.label ?? 'running'}</span></div>
          <Terminal lines={lines} height={220} />
        </div>
      )}

      <div className="grid-camp">
        <div className="card">
          <div className="card-head">
            <h2>1 · Planner output</h2>
            <select value={planSel ?? ''} onChange={(e) => setPlanSel(e.target.value)}>
              {(plans ?? []).map((x) => <option key={x.id} value={x.id}>{shortId(x.id)} · {x.experiments} exp · {x.source}</option>)}
            </select>
          </div>
          {plan ? <PlanView plan={plan} graph={graph} /> : (
            <div className="empty">No plan yet. <button className="btn" onClick={goLive}>Open Live Lab and let the AI plan →</button></div>
          )}
        </div>

        <div className="card">
          <div className="card-head">
            <h2>2 · Campaign record</h2>
            <select value={sel ?? ''} onChange={(e) => setSel(e.target.value)}>
              {(list ?? []).map((x) => <option key={x.id} value={x.id}>{shortId(x.id)} · {x.runs} runs</option>)}
            </select>
          </div>
          {!c ? <div className="empty">No campaigns in ./results/campaigns yet.</div> : (
            <>
              <table className="tbl">
                <thead><tr><th>#</th><th>Experiment</th><th>Predicted</th><th>Observed</th><th>Verdict</th></tr></thead>
                <tbody>
                  {c.entries.map((e) => (
                    <tr key={e.run_id} className="click" onClick={() => onOpenRun(e.run_id)}>
                      <td>{e.rank}</td>
                      <td><b>{TYPE_INFO[e.spec.type]?.label}</b> → {e.spec.target}<small>{e.spec.duration_s}s{e.spec.origin === 'adapter' ? ' · follow-up added by the adapter' : ''}</small></td>
                      <td><small>{e.spec.prediction?.error_band} errors · {e.spec.prediction?.slowdown_band} slowdown</small></td>
                      <td>{pct(e.peak_error_rate)} err · {ms(e.peak_p95_ms)}<small>{e.status}{e.recovery_s != null ? ` · recovery ${e.recovery_s}s` : ''}</small></td>
                      <td><span className={`pill ${verdictTone[e.prediction] ?? ''}`}>{e.prediction ?? '-'}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <h3 className="sub">3 · Adapter decisions</h3>
              <ol className="decisions">
                {(c.decisions ?? []).map((d, i) => (
                  <li key={i}>
                    <span className={`pill ${d.action === 'continue' ? 'ok' : 'warn'}`}>{d.action}</span>
                    <p>{d.reason}</p>
                    <small>after {shortId(d.after_run)} · {d.detail}</small>
                  </li>
                ))}
              </ol>
              <p className="ended">Ended: {c.status}</p>
              <button className="btn ghost" onClick={onOpenReport}>4 · Open the analyst's findings →</button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
