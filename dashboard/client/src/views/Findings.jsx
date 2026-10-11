import React, { useEffect, useState } from 'react';
import { useFetch, shortId } from '../api.js';

const SEV = { critical: 0, high: 1, medium: 2, low: 3 };
const DIMS = [['availability', 'Avail.'], ['latency', 'Latency'], ['recovery', 'Recovery'], ['containment', 'Contain.'], ['posture', 'Posture']];
const gradeTone = (g) => ({ A: 'ok', B: 'ok', C: 'warn', D: 'warn', F: 'bad' }[g] ?? '');
const scoreTone = (s) => (s >= 80 ? 'ok' : s >= 60 ? 'warn' : 'bad');

function Ring({ score, grade }) {
  const r = 54, c = 2 * Math.PI * r;
  return (
    <div className={`ring ${scoreTone(score)}`}>
      <svg viewBox="0 0 130 130">
        <circle cx="65" cy="65" r={r} className="track" />
        <circle cx="65" cy="65" r={r} className="val" strokeDasharray={c} strokeDashoffset={c * (1 - score / 100)} transform="rotate(-90 65 65)" />
      </svg>
      <div className="ring-text"><b>{Math.round(score)}</b><span>/ 100</span><em className={gradeTone(grade)}>grade {grade}</em></div>
    </div>
  );
}

function fixText(f) {
  if (!f) return '';
  const { target, action, ...rest } = f;
  const extra = Object.entries(rest).filter(([, v]) => v != null).map(([k, v]) => `${k}: ${typeof v === 'object' ? JSON.stringify(v) : v}`).join(' · ');
  return `${action.replace(/_/g, ' ')} on ${target}${extra ? ` (${extra})` : ''}`;
}

export default function Findings({ onOpenRun }) {
  const [list] = useFetch('/reports');
  const [sel, setSel] = useState(null);
  const [data] = useFetch(sel ? `/reports/${sel}` : null);
  const [open, setOpen] = useState(0);
  useEffect(() => { if (!sel && list?.length) setSel(list[0].id); }, [list, sel]);

  if (list && !list.length) return <div className="view"><div className="card empty">No reports in ./results/reports. Run <code>chaos-agent analyze</code> on the VM and sync.</div></div>;
  const a = data?.analysis;
  if (!a) return <div className="view"><div className="card empty">loading report...</div></div>;
  const findings = [...(a.ai?.findings ?? [])].sort((x, y) => SEV[x.severity] - SEV[y.severity]);
  const services = [...(a.services ?? [])].sort((x, y) => x.score - y.score);
  const pr = a.predictions ?? {};

  return (
    <div className="view findings">
      <div className="card summary">
        <Ring score={a.overall_score} grade={a.grade} />
        <div className="sum-body">
          <div className="card-head">
            <h2>Resilience score</h2>
            <select value={sel ?? ''} onChange={(e) => { setSel(e.target.value); setOpen(0); }}>
              {(list ?? []).map((x) => <option key={x.id} value={x.id}>{shortId(x.id)} · {Math.round(x.overall_score)}</option>)}
            </select>
          </div>
          <p>{a.ai?.summary}</p>
          <div className="facts">
            <span><b>{a.coverage?.experiments}</b> experiments</span>
            <span><b>{a.coverage?.types?.length}</b> failure types</span>
            <span><b>{findings.length}</b> findings</span>
            <span>predictions <b className="ok">{pr.confirmed} confirmed</b> · <b className="warn">{pr.partly} partly</b> · <b className="bad">{pr.refuted} refuted</b></span>
            <span>anchor (code) score <b>{a.overall_anchor}</b> · AI kept within ±15</span>
          </div>
        </div>
      </div>

      <div className="grid-find">
        <div className="col">
          <h2 className="sec">Findings, ranked</h2>
          {findings.map((f, i) => (
            <article key={i} className={`card finding ${f.severity} ${open === i ? 'open' : ''}`}>
              <button className="f-head" onClick={() => setOpen(open === i ? -1 : i)}>
                <span className={`sev ${f.severity}`}>{f.severity}</span>
                <span className="f-title">{f.title}</span>
                <span className="f-svcs">{f.services?.join(' · ')}</span>
              </button>
              {open === i && (
                <div className="f-body">
                  <h4>Evidence</h4><p>{f.evidence}</p>
                  <h4>Root cause</h4><p>{f.root_cause}</p>
                  <h4>Fix</h4>
                  <p className="fix"><code>{fixText(f.fix)}</code></p>
                  <p>{f.fix_rationale}</p>
                  <h4>Expected improvement</h4><p>{f.expected_improvement}</p>
                  <div className="runs">
                    {f.evidence_runs?.map((r) => <button key={r} className="chip btn-chip" onClick={() => onOpenRun(r)}>▶ {shortId(r)}</button>)}
                  </div>
                </div>
              )}
            </article>
          ))}
        </div>

        <div className="col">
          <h2 className="sec">Per-service scores</h2>
          <div className="card svc-scores">
            {services.map((s) => (
              <div key={s.service} className={`svc ${s.tested ? '' : 'untested'}`}>
                <div className="svc-top"><b>{s.service}</b><span className={`score ${scoreTone(s.score)}`}>{Math.round(s.score)} <em>{s.grade}</em></span></div>
                <div className="bar"><i className={scoreTone(s.score)} style={{ width: `${s.score}%` }} /></div>
                <div className="dims">
                  {DIMS.map(([k, l]) => <span key={k} title={`${l}: ${s.dimensions?.[k]}`}><i style={{ height: `${s.dimensions?.[k] ?? 0}%` }} className={scoreTone(s.dimensions?.[k] ?? 0)} /><em>{l}</em></span>)}
                </div>
                {!s.tested && <small>not tested - score from design only</small>}
              </div>
            ))}
          </div>
          <h2 className="sec">Limits of this report</h2>
          <div className="card"><ul className="limits">{a.ai?.limitations?.map((l, i) => <li key={i}>{l}</li>)}</ul></div>
        </div>
      </div>
    </div>
  );
}
