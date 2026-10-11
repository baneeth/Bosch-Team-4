import React from 'react';

// Fixed layout for the Online Boutique call graph (viewBox 900 x 500). Unknown services are appended in a row.
const POS = {
  frontend: [80, 250],
  adservice: [330, 50], recommendationservice: [330, 140], cartservice: [330, 230], currencyservice: [330, 320], checkoutservice: [330, 430],
  'productcatalogservice': [630, 70], 'redis-cart': [630, 190], shippingservice: [630, 290], emailservice: [630, 370], paymentservice: [630, 450],
};
const W = 176, H = 50;

function podState(pods) {
  if (!pods?.length) return { tone: 'unknown', text: 'no pod' };
  const ready = pods.filter((p) => p.ready && !p.deleting).length;
  const reason = pods.map((p) => p.reason).find(Boolean);
  if (pods.some((p) => p.deleting)) return { tone: 'bad', text: 'terminating' };
  if (ready === 0) return { tone: 'bad', text: reason || 'not ready' };
  if (ready < pods.length) return { tone: 'warn', text: `${ready}/${pods.length} ready` };
  return { tone: 'ok', text: `${ready}/${pods.length} ready` };
}

export default function Topology({ graph, pods, metrics, targets, onPick }) {
  const nodes = Object.values(graph?.nodes ?? {}).filter((n) => n.role !== 'load' && n.name !== 'loadgenerator' && n.exists !== false);
  if (!nodes.length) return <div className="empty">dependency map not found (run <code>chaos-agent graph</code> and sync results)</div>;
  const byName = Object.fromEntries(nodes.map((n) => [n.name, n]));
  let extra = 0;
  const pos = (n) => POS[n.name] ?? [80 + (extra++ % 6) * 150, 480];
  const P = Object.fromEntries(nodes.map((n) => [n.name, pos(n)]));
  const podsBy = {};
  for (const p of pods ?? []) (podsBy[p.service] ||= []).push(p);

  const edges = [];
  for (const n of nodes) for (const d of n.depends_on ?? []) if (byName[d]) edges.push([n.name, d]);

  return (
    <svg className="topology" viewBox="0 0 900 500" role="img" aria-label="service dependency map">
      <defs>
        <marker id="arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
          <path d="M0,0 L8,4 L0,8z" fill="var(--line-strong)" />
        </marker>
      </defs>
      {edges.map(([a, b]) => {
        const [ax, ay] = P[a], [bx, by] = P[b];
        const ex = bx > ax ? bx - W / 2 : bx + W / 2;
        const x1 = bx > ax ? ax + W / 2 : ax - W / 2;
        const mid = (x1 + ex) / 2;
        const hot = targets.has(b) || targets.has(a);
        return <path key={`${a}-${b}`} className={`edge ${hot ? 'hot' : ''}`} d={`M${x1},${ay} C${mid},${ay} ${mid},${by} ${ex},${by}`} markerEnd="url(#arrow)" />;
      })}
      {nodes.map((n) => {
        const [x, y] = P[n.name];
        const st = podState(podsBy[n.name]);
        const m = metrics?.services?.[n.name];
        const limit = parseFloat(n.cpu_limit) / (String(n.cpu_limit).endsWith('m') ? 1000 : 1);
        const cpuFrac = m?.cpuCores != null && limit ? Math.min(1, m.cpuCores / limit) : null;
        const target = targets.has(n.name);
        const restarts = (podsBy[n.name] ?? []).reduce((s, p) => s + p.restarts, 0);
        return (
          <g key={n.name} className={`node ${st.tone} ${target ? 'target' : ''}`} transform={`translate(${x - W / 2},${y - H / 2})`} onClick={() => onPick?.(n.name)}>
            {target && <rect className="halo" x="-5" y="-5" width={W + 10} height={H + 10} rx="14" />}
            <rect className="box" width={W} height={H} rx="10" />
            <circle className="dot" cx="14" cy="16" r="5" />
            <text className="name" x="26" y="20">{n.name.endsWith('service') ? n.name.slice(0, -7) : n.name}{n.name.endsWith('service') && <tspan className="svc">service</tspan>}</text>
            <text className="sub" x="12" y="39">{target ? 'FAULT INJECTED' : st.text}{restarts ? ` · ${restarts}↻` : ''}</text>
            {cpuFrac != null && (
              <g>
                <rect className="cpu-bg" x={W - 44} y="32" width="34" height="5" rx="2.5" />
                <rect className={`cpu ${cpuFrac > 0.85 ? 'hot' : ''}`} x={W - 44} y="32" width={34 * cpuFrac} height="5" rx="2.5" />
              </g>
            )}
          </g>
        );
      })}
    </svg>
  );
}
