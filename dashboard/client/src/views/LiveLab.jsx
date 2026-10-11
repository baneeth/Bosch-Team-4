import React, { useMemo } from 'react';
import Storefront from '../components/Storefront.jsx';
import PipelinePanel from '../components/PipelinePanel.jsx';
import { currentRun, TYPE_INFO } from '../meta.js';
import Terminal from '../components/Terminal.jsx';
import Topology from '../components/Topology.jsx';
import LineChart from '../components/LineChart.jsx';
import { clock, ms, pct } from '../api.js';
import ResourceCard from '../components/ResourceCard.jsx';

const STEPS = ['Observe', 'Inject', 'Impact', 'Recover', 'Done'];

function parseProgress(allLines, job) {
  let from = 0;
  allLines.forEach((x, k) => { if (/\brun \d{8}-\d{6}-[a-z_]+-[\w-]+: /.test(x)) from = k; });
  const lines = allLines.slice(from);
  let phase = null, last = null;
  for (let i = lines.length - 1; i >= 0; i--) {
    const m = lines[i].match(/^\s+(pre|fault|recovery)\s+t\+\s*([\d.]+)s n=\s*(\d+) err=\s*([\d.]+)% p95=\s*([\d.]+|-)/);
    if (m) { phase = m[1]; last = { t: Number(m[2]), err: Number(m[4]), p95: m[5] === '-' ? null : Number(m[5]) }; break; }
  }
  const text = lines.join('\n');
  const injected = /INJECTED/.test(text);
  const aborted = /^\d\d:\d\d:\d\d ABORTED/m.test(text);
  let step = -1;
  if (job) step = 0;
  if (phase === 'pre') step = 0;
  if (injected) step = 1;
  if (phase === 'fault') step = 2;
  if (/cleaned up|fault ended|measuring recovery/.test(text) && phase === 'recovery') step = 3;
  if (job && job.status !== 'running') step = 4;
  if (/^\d\d:\d\d:\d\d (COMPLETED|ABORTED)/m.test(text)) step = 4;
  return { step, last, aborted, injected };
}

export default function LiveLab({ ctx }) {
  const { config, graph, pods, metrics, mhist, health, jobState } = ctx;
  const { snap, series, offline } = health;
  const { job, lines, markers } = jobState;
  const running = job?.status === 'running';
  const prog = useMemo(() => parseProgress(lines, job), [lines, job]);
  const cur = useMemo(() => currentRun(lines), [lines]);

  const targets = useMemo(() => {
    const t = new Set();
    for (const c of pods?.chaos ?? []) {
      const sel = c.targets;
      const v = sel?.app ?? Object.values(sel ?? {}).find((x) => typeof x === 'string');
      if (v && !c.deleting) t.add(v);
    }
    const cr = currentRun(lines);
    if (running && cr && !cr.done) t.add(cr.target);   // k6 traffic spikes have no Chaos Mesh object
    return t;
  }, [pods, running, lines]);

  const now = snap?.now ?? Date.now();
  const x0 = now - 120000;
  const errSeries = series.map((p) => ({ x: p.t, y: p.n ? p.errorRate * 100 : null }));
  const latSeries = series.map((p) => ({ x: p.t, y: p.p95 }));
  const fmtX = (v) => `${Math.round((v - now) / 1000)}s`;
  const zones = [];
  markers.forEach((m, i) => {          // one shaded zone per injection (a campaign injects several)
    if (m.label !== 'fault injected') return;
    const end = markers.slice(i + 1).find((x) => x.label === 'fault removed' || x.label === 'guard abort');
    zones.push({ from: m.x, to: end ? end.x : now });
  });
  const breached = prog.aborted;
  const maxErr = 20;

  return (
    <div className={`view live ${snap?.state ?? ''}`}>
      <div className="grid-top">
        <Storefront snap={snap} offline={offline} shopUrl={config?.shopUrl} frameUrl={config?.proxyUrl} />
        <PipelinePanel jobState={jobState} goAgents={ctx.goAgents} />
      </div>

      <div className="grid-mid">
        <div className="card">
          <div className="card-head"><h2>User-facing error rate</h2><span className="chip">last 2 min</span></div>
          <LineChart data={errSeries} xDomain={[x0, now]} color="var(--bad)" yMax={Math.max(25, ...errSeries.map((d) => d.y ?? 0)) * 1.05}
            threshold={{ y: maxErr, label: 'guard: 20 %' }} markers={markers} zones={zones} fmtY={(v) => `${Math.round(v)}%`} fmtX={fmtX} />
        </div>
        <div className="card">
          <div className="card-head"><h2>Page latency (p95)</h2><span className="chip">last 2 min</span></div>
          <LineChart data={latSeries} xDomain={[x0, now]} color="var(--accent)" threshold={{ y: 1500, label: 'guard: 1.5 s' }}
            markers={markers} zones={zones} fmtY={ms} fmtX={fmtX} />
        </div>
        <ResourceCard graph={graph} mhist={mhist} target={[...targets][0]} now={now} markers={markers} zones={zones} />
      </div>

      <div className="grid-bottom">
        <div className="card">
          <div className="card-head">
            <h2>Agent activity</h2>
            {cur && running && !cur.done && <span className="chip">now: {TYPE_INFO[cur.type]?.label ?? cur.type} → {cur.target}</span>}
            {job && <span className={`pill ${job.status}`}>{job.status}</span>}
          </div>
          <ol className="steps">
            {STEPS.map((s, i) => (
              <li key={s} className={i < prog.step ? 'done' : i === prog.step ? (prog.aborted && i === 4 ? 'abort' : 'now') : ''}>{s}</li>
            ))}
          </ol>
          {prog.last && running && (
            <div className="live-nums">t+{prog.last.t.toFixed(0)}s · errors {prog.last.err}% · p95 {prog.last.p95 ?? '-'} ms</div>
          )}
          <div className={`guard ${breached ? 'tripped' : 'armed'}`}>
            <b>{breached ? 'Safety guard tripped - fault removed' : 'Safety guard armed'}</b>
            <span>Stops and rolls back automatically: errors above 20 %, p95 above max(3x baseline, 1.5 s), frontend silent 3 s, or any non-target pod NotReady. Deterministic code - not the AI.</span>
          </div>
          <Terminal lines={lines} height="auto" grow />
          {jobState.error && <div className="alert">{jobState.error}</div>}
        </div>

        <div className="card wide">
          <div className="card-head">
            <h2>Service map</h2>
            <div className="legend"><i className="ok" />healthy <i className="warn" />degraded <i className="bad" />down <i className="hit" />fault target</div>
          </div>
          <Topology graph={graph} pods={pods?.pods} metrics={metrics} targets={targets} />
          <div className="map-foot">
            <span>VM CPU {metrics?.vmCpu != null ? pct(metrics.vmCpu) : '-'}</span>
            <span>{pods?.pods?.length ?? 0} pods</span>
            <span>{pods?.chaos?.length ? `${pods.chaos.length} active chaos object(s)` : 'no active chaos objects'}</span>
            {snap && <span>updated {clock(snap.now)}</span>}
          </div>
        </div>
      </div>
    </div>
  );
}
