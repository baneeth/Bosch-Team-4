import React from 'react';
import { TYPE_INFO } from '../meta.js';

const paramText = (p) => Object.entries(p || {}).map(([k, v]) => `${k}=${v}`).join(', ');

/** The planner agent's ranked experiments, each with its reason and the dependency-map facts behind it. */
export default function PlanView({ plan, graph, compact = false }) {
  if (!plan) return <div className="empty">No plan yet.</div>;
  const nodes = graph?.nodes ?? {};
  const rejected = plan.rejected ?? [];
  return (
    <div className={`plan ${compact ? 'compact' : ''}`}>
      {!compact && (
        <div className="strategy">
          <b>Planner's strategy</b>
          <p>{plan.strategy}</p>
          <small>{plan.source === 'ai' ? `AI plan · ${plan.model}` : 'labelled fallback plan (no AI call)'}</small>
        </div>
      )}
      <ol className="plan-list">
        {plan.experiments.map((e) => {
          const n = nodes[e.spec.target];
          const pr = e.spec.prediction;
          return (
            <li key={e.rank} className="plan-item">
              <span className="rank">{e.rank}</span>
              <div className="plan-main">
                <div className="plan-title">
                  <span className={`type-tag ${TYPE_INFO[e.spec.type]?.cls}`}>{TYPE_INFO[e.spec.type]?.label ?? e.spec.type}</span>
                  <b>{e.spec.target}</b>
                  <small>{e.spec.duration_s}s{paramText(e.spec.params) ? ` · ${paramText(e.spec.params)}` : ''}</small>
                </div>
                <p className="why"><b>Why:</b> {e.spec.reason}</p>
                {!compact && e.spec.hypothesis && <p className="hyp"><b>Hypothesis:</b> {e.spec.hypothesis}</p>}
                <div className="plan-facts">
                  {n && <span className="chip" title="from the dependency map">risk {n.risk} · reaches {Math.round((n.reach ?? 0) * 100)}% of user flows · {n.replicas} replica{n.replicas === 1 ? '' : 's'}</span>}
                  {pr && <span className="chip">predicts {pr.error_band} errors · {pr.slowdown_band} slowdown</span>}
                  {pr?.affected_flows?.length > 0 && !compact && <span className="chip">flows: {pr.affected_flows.join(', ')}</span>}
                </div>
              </div>
            </li>
          );
        })}
      </ol>
      {rejected.length > 0 && (
        <details className="rejected">
          <summary>{rejected.length} AI proposal{rejected.length === 1 ? '' : 's'} rejected by the safety policy</summary>
          <ul>{rejected.map((r, i) => <li key={i}>{typeof r === 'string' ? r : r.reason || r.error || JSON.stringify(r).slice(0, 200)}</li>)}</ul>
        </details>
      )}
    </div>
  );
}
