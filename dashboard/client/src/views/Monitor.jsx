import React from 'react';
import { pct } from '../api.js';

const cores = (s) => (s.endsWith('m') ? parseFloat(s) / 1000 : parseFloat(s));
const mib = (s) => (s.endsWith('Gi') ? parseFloat(s) * 1024 : parseFloat(s));

export default function Monitor({ ctx }) {
  const { metrics, pods, graph, config } = ctx;
  const nodes = Object.values(graph?.nodes ?? {}).filter((n) => n.name !== 'loadgenerator' && n.exists !== false);
  const rows = nodes.map((n) => {
    const m = metrics?.services?.[n.name] ?? {};
    const cpuLim = n.cpu_limit ? cores(n.cpu_limit) : null, memLim = n.memory_limit ? mib(n.memory_limit) : null;
    const ps = (pods?.pods ?? []).filter((p) => p.service === n.name);
    return { name: n.name, cpu: m.cpuCores, cpuFrac: cpuLim && m.cpuCores != null ? m.cpuCores / cpuLim : null, mem: m.memMiB,
      memFrac: memLim && m.memMiB != null ? m.memMiB / memLim : null, throttle: m.throttle, restarts: ps.reduce((s, p) => s + p.restarts, 0),
      ready: ps.length && ps.every((p) => p.ready) };
  }).sort((a, b) => (b.cpuFrac ?? 0) - (a.cpuFrac ?? 0));
  const tone = (f) => (f == null ? '' : f > 0.9 ? 'bad' : f > 0.7 ? 'warn' : 'ok');

  return (
    <div className="view monitor">
      <div className="card">
        <div className="card-head">
          <h2>Resource monitor <span className="chip">Prometheus · 5 s scrape</span></h2>
          <div className="btn-row">
            {config?.grafanaUrl && <a className="btn" href={`${config.grafanaUrl}/d/chaos-lab`} target="_blank" rel="noreferrer">Open Grafana dashboard ↗</a>}
            {config?.promUrl && <a className="btn ghost" href={config.promUrl} target="_blank" rel="noreferrer">Prometheus ↗</a>}
          </div>
        </div>
        <div className="vm-cpu">
          <span>VM CPU (4 vCPU)</span>
          <div className="bar big"><i className={tone(metrics?.vmCpu)} style={{ width: `${(metrics?.vmCpu ?? 0) * 100}%` }} /></div>
          <b>{metrics?.vmCpu != null ? pct(metrics.vmCpu) : '-'}</b>
        </div>
        <table className="tbl res">
          <thead><tr><th>Service</th><th>State</th><th>CPU vs limit</th><th>Memory vs limit</th><th>CPU throttled</th><th>Restarts</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.name}>
                <td><b>{r.name}</b></td>
                <td><span className={`dot ${r.ready ? 'ok' : 'bad'}`} /> {r.ready ? 'ready' : 'not ready'}</td>
                <td><div className="bar"><i className={tone(r.cpuFrac)} style={{ width: `${Math.min(100, (r.cpuFrac ?? 0) * 100)}%` }} /></div><small>{r.cpu != null ? `${(r.cpu * 1000).toFixed(0)}m` : '-'} {r.cpuFrac != null && `(${pct(r.cpuFrac)})`}</small></td>
                <td><div className="bar"><i className={tone(r.memFrac)} style={{ width: `${Math.min(100, (r.memFrac ?? 0) * 100)}%` }} /></div><small>{r.mem != null ? `${r.mem.toFixed(0)} MiB` : '-'} {r.memFrac != null && `(${pct(r.memFrac)})`}</small></td>
                <td className={r.throttle > 0.5 ? 'bad' : ''}>{r.throttle != null && !Number.isNaN(r.throttle) ? pct(r.throttle) : '-'}</td>
                <td className={r.restarts ? 'warn' : ''}>{r.restarts}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {!metrics && <div className="empty">waiting for Prometheus...</div>}
      </div>

    </div>
  );
}
