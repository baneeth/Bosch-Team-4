import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useFetch, ms, pct, shortId } from '../api.js';
import { TYPE_INFO, FLOWS } from '../meta.js';
import LineChart from '../components/LineChart.jsx';
import Terminal from '../components/Terminal.jsx';

const stateOf = (err, p95) => (err >= 0.5 ? 'down' : err > 0.05 || (p95 ?? 0) > 800 ? 'degraded' : 'healthy');
const STATE_LABEL = { healthy: 'Shop healthy', degraded: 'Shop degraded', down: 'Shop DOWN' };

export default function Replay({ initialRun, onOpenRun }) {
  const [runs] = useFetch('/runs');
  const [sel, setSel] = useState(initialRun || null);
  const [filter, setFilter] = useState('all');
  const [detail] = useFetch(sel ? `/runs/${sel}` : null);
  const [cursor, setCursor] = useState(null);       // seconds since injection
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(4);
  const timer = useRef(null);

  useEffect(() => { if (initialRun) setSel(initialRun); }, [initialRun]);
  useEffect(() => { if (!sel && runs?.length) setSel(runs[0].id); }, [runs, sel]);

  const tl = detail?.timeline ?? [];
  const r = detail?.result;
  const tMin = tl[0]?.t_since_inject_s ?? -20, tMax = tl[tl.length - 1]?.t_since_inject_s ?? 60;

  useEffect(() => { setCursor(null); setPlaying(false); }, [sel]);
  useEffect(() => {
    clearInterval(timer.current);
    if (!playing) return undefined;
    timer.current = setInterval(() => {
      setCursor((c) => {
        const next = (c ?? tMin) + 0.25 * speed;
        if (next >= tMax) { setPlaying(false); return tMax; }
        return next;
      });
    }, 250);
    return () => clearInterval(timer.current);
  }, [playing, speed, tMin, tMax]);

  const filtered = (runs ?? []).filter((x) => filter === 'all' || x.status === filter || TYPE_INFO[x.type]?.cls === filter);
  const err = useMemo(() => tl.map((p) => ({ x: p.t_since_inject_s, y: p.error_rate == null ? null : p.error_rate * 100 })), [tl]);
  const lat = useMemo(() => tl.map((p) => ({ x: p.t_since_inject_s, y: p.p95_ms })), [tl]);
  const at = cursor == null ? null : tl.reduce((best, p) => (Math.abs(p.t_since_inject_s - cursor) < Math.abs((best?.t_since_inject_s ?? 1e9) - cursor) ? p : best), null);
  const faultEnd = r?.fault_duration_s ? r.fault_duration_s : r?.spec?.duration_s;
  const zones = [{ from: 0, to: faultEnd ?? 0 }];
  const breach = tl.find((p) => p.breaches);
  const markers = [{ x: 0, label: 'fault injected', tone: 'bad' }];
  if (breach) markers.push({ x: breach.t_since_inject_s, label: 'guard breach', tone: 'bad' });
  if (r?.recovery_s != null && faultEnd != null) markers.push({ x: faultEnd + r.recovery_s, label: 'recovered', tone: 'good' });

  const shownEvents = useMemo(() => {
    if (!detail) return [];
    if (cursor == null || !detail.events.length) return detail.events;
    const t0 = Date.parse(r.injected_at);
    return detail.events.filter((e) => {
      const m = e.match(/^(\S+)\s/);
      return !m || (Date.parse(m[1]) - t0) / 1000 <= cursor;
    });
  }, [detail, cursor, r]);

  const liveState = at ? stateOf(at.error_rate ?? 0, at.p95_ms) : null;

  return (
    <div className="view replay">
      <aside className="card runlist">
        <div className="card-head"><h2>Experiments <span className="count">{filtered.length}</span></h2></div>
        <div className="chips">
          {['all', 'COMPLETED', 'ABORTED', 'errors', 'slowdown', 'resource', 'load'].map((f) => (
            <button key={f} className={`chip btn-chip ${filter === f ? 'on' : ''}`} onClick={() => setFilter(f)}>{f === 'all' ? 'all' : f.toLowerCase()}</button>
          ))}
        </div>
        <div className="rows">
          {filtered.map((x) => (
            <button key={x.id} className={`row ${sel === x.id ? 'on' : ''}`} onClick={() => setSel(x.id)}>
              <span className={`dot ${x.status === 'COMPLETED' ? 'ok' : x.status === 'ABORTED' ? 'warn' : 'bad'}`} />
              <span className="main"><b>{TYPE_INFO[x.type]?.label ?? x.type}</b> → {x.target}</span>
              <span className="meta">{shortId(x.id).split(' ')[0]} · {pct(x.peak_error_rate)} err · {ms(x.peak_p95_ms)}</span>
            </button>
          ))}
          {!runs && <div className="empty">loading...</div>}
          {runs && !runs.length && <div className="empty">No runs found in ./results/runs</div>}
        </div>
      </aside>

      <section className="detail">
        {!r ? <div className="card empty">Select an experiment to replay it.</div> : (
          <>
            <div className={`card hero ${liveState ?? ''}`}>
              <div className="hero-top">
                <div>
                  <span className={`type-tag ${TYPE_INFO[r.spec.type]?.cls}`}>{TYPE_INFO[r.spec.type]?.label}</span>
                  <h2>{r.spec.target} <small>for {r.spec.duration_s}s {Object.keys(r.spec.params || {}).length ? `· ${Object.entries(r.spec.params).map(([k, v]) => `${k}=${v}`).join(', ')}` : ''}</small></h2>
                  <p className="reason">{r.spec.reason}</p>
                </div>
                <span className={`pill ${r.status}`}>{r.status}</span>
              </div>
              <div className="kpi-grid">
                <div><dt>Peak error rate</dt><dd className={r.peak_error_rate > 0.2 ? 'bad' : ''}>{pct(r.peak_error_rate, 1)}</dd></div>
                <div><dt>Peak p95</dt><dd className={r.peak_p95_ms > 1500 ? 'bad' : ''}>{ms(r.peak_p95_ms)}</dd></div>
                <div><dt>Recovery</dt><dd>{r.recovery_s == null ? 'not recovered' : `${r.recovery_s.toFixed(1)} s`}</dd></div>
                <div><dt>Pod ready after</dt><dd>{r.pod_ready_s == null ? '-' : `${r.pod_ready_s.toFixed(1)} s`}</dd></div>
                <div><dt>Worst flow</dt><dd>{r.worst_flow ?? '-'}</dd></div>
                <div><dt>Guard limits</dt><dd>{pct(r.thresholds.max_error_rate)} · {ms(r.thresholds.max_p95_ms)}</dd></div>
              </div>
              {r.abort_reason && <div className="alert warn"><b>Guard stopped it:</b> {r.abort_reason}</div>}
            </div>

            <div className="card">
              <div className="card-head">
                <h2>Replay</h2>
                <div className="player">
                  <button className="btn" onClick={() => { if (!playing && (cursor == null || cursor >= tMax)) setCursor(tMin); setPlaying((p) => !p); }}>{playing ? 'Pause' : cursor == null || cursor >= tMax ? 'Play' : 'Resume'}</button>
                  <select value={speed} onChange={(e) => setSpeed(Number(e.target.value))}>
                    {[1, 2, 4, 8].map((s) => <option key={s} value={s}>{s}x</option>)}
                  </select>
                  <input type="range" min={tMin} max={tMax} step="0.25" value={cursor ?? tMin} onChange={(e) => { setPlaying(false); setCursor(Number(e.target.value)); }} />
                  <span className="time">{cursor == null ? '' : `${cursor >= 0 ? '+' : ''}${cursor.toFixed(1)}s`}</span>
                </div>
              </div>
              {liveState && (
                <div className={`replay-banner ${liveState}`}>
                  <span className="beacon-sm" />
                  <b>{STATE_LABEL[liveState]}</b>
                  <span>errors {pct(at.error_rate, 0)} · p95 {ms(at.p95_ms)} · phase {at.phase}</span>
                </div>
              )}
              <div className="two">
                <div><h3>Error rate</h3><LineChart data={err} xDomain={[tMin, tMax]} color="var(--bad)" threshold={{ y: (r.thresholds.max_error_rate ?? 0.2) * 100, label: 'guard' }}
                  zones={zones} markers={markers} cursor={cursor} yMax={Math.max(25, ...err.map((d) => d.y ?? 0)) * 1.05} fmtY={(v) => `${Math.round(v)}%`} fmtX={(v) => `${Math.round(v)}s`} /></div>
                <div><h3>p95 latency</h3><LineChart data={lat} xDomain={[tMin, tMax]} color="var(--accent)" threshold={{ y: r.thresholds.max_p95_ms, label: 'guard' }}
                  zones={zones} markers={markers} cursor={cursor} fmtY={ms} fmtX={(v) => `${Math.round(v)}s`} /></div>
              </div>
            </div>

            <div className="two-col">
              <div className="card">
                <div className="card-head"><h2>Impact per user flow</h2><span className="chip">baseline → during fault</span></div>
                <table className="tbl">
                  <thead><tr><th>Flow</th><th>p95 before</th><th>p95 during</th><th>×</th><th>Errors during</th></tr></thead>
                  <tbody>
                    {FLOWS.map(([id, label]) => {
                      const f = r.flow_impact?.[id];
                      if (!f) return null;
                      const bad = f.impact.error_rate > 0.05, slow = f.p95_factor > 2;
                      return (
                        <tr key={id}>
                          <td>{label}</td><td>{ms(f.baseline.p95_ms)}</td><td>{ms(f.impact.p95_ms)}</td>
                          <td className={slow ? 'warn' : ''}>{f.p95_factor?.toFixed(1)}</td>
                          <td className={bad ? 'bad' : ''}>{pct(f.impact.error_rate)} <small>({f.impact.errors}/{f.impact.count})</small></td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              <div className="card">
                <div className="card-head"><h2>Agent event log</h2></div>
                <Terminal lines={shownEvents.map((e) => e.replace(/^\S+\s/, ''))} height={250} placeholder="no events" />
              </div>
            </div>
            
          </>
        )}
      </section>
    </div>
  );
}
