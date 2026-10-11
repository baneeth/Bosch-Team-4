import React from 'react';
import LineChart from './LineChart.jsx';

const cores = (s) => (String(s).endsWith('m') ? parseFloat(s) / 1000 : parseFloat(s));
const mib = (s) => (String(s).endsWith('Gi') ? parseFloat(s) * 1024 : parseFloat(s));

/** CPU and memory (as % of the container limit) of the service under attack - makes CPU / memory stress visible live. */
export default function ResourceCard({ graph, mhist, target, now, markers, zones }) {
  // no fault running: show the busiest service instead
  const last = mhist[mhist.length - 1]?.services ?? {};
  const svc = target || Object.entries(last).sort((a, b) => (b[1].cpuCores ?? 0) - (a[1].cpuCores ?? 0))[0]?.[0];
  const node = graph?.nodes?.[svc];
  const cpuLim = node?.cpu_limit ? cores(node.cpu_limit) : null;
  const memLim = node?.memory_limit ? mib(node.memory_limit) : null;
  const cpu = mhist.map((h) => ({ x: h.t, y: cpuLim && h.services[svc]?.cpuCores != null ? Math.min(150, (h.services[svc].cpuCores / cpuLim) * 100) : null }));
  const mem = mhist.map((h) => ({ x: h.t, y: memLim && h.services[svc]?.memMiB != null ? Math.min(150, (h.services[svc].memMiB / memLim) * 100) : null }));
  const x0 = now - 120000;
  const fmtX = (v) => `${Math.round((v - now) / 1000)}s`;
  const pct = (v) => `${Math.round(v)}%`;
  return (
    <div className="card">
      <div className="card-head">
        <h2>Resource pressure</h2>
        <span className="chip">{svc ?? '-'}{target ? ' · under attack' : ''}</span>
      </div>
      <h3>CPU (% of limit)</h3>
      <LineChart data={cpu} xDomain={[x0, now]} height={105} color="var(--accent)" yMax={110} threshold={{ y: 100, label: 'limit' }}
        markers={markers} zones={zones} fmtY={pct} fmtX={fmtX} empty="waiting for Prometheus..." />
      <h3>Memory (% of limit)</h3>
      <LineChart data={mem} xDomain={[x0, now]} height={105} color="var(--ink)" yMax={110} threshold={{ y: 100, label: 'OOM kill' }}
        markers={[]} zones={zones} fmtY={pct} fmtX={fmtX} empty="waiting for Prometheus..." />
    </div>
  );
}
