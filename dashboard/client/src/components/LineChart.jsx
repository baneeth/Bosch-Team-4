import React from 'react';
import { useWidth } from '../api.js';

/**
 * Small dependency-free SVG line chart.
 *  data:       [{x, y}] (y may be null -> gap)
 *  xDomain:    [min, max]
 *  threshold:  {y, label}  dashed guard line
 *  markers:    [{x, label, tone}] vertical lines (injection, revert, abort)
 *  zones:      [{from, to}] shaded x ranges (fault active)
 *  cursor:     x position of the replay cursor
 */
export default function LineChart({
  data, xDomain, height = 170, color = 'var(--accent)', yMin = 0, yMax, threshold, markers = [], zones = [],
  cursor, fmtY = (v) => v, fmtX = (v) => v, empty = 'waiting for data...',
}) {
  const [ref, w] = useWidth();
  const pad = { l: 46, r: 12, t: 10, b: 22 };
  const iw = w - pad.l - pad.r, ih = height - pad.t - pad.b;
  const pts = data.filter((d) => d.y != null);
  const top = yMax ?? Math.max(threshold ? threshold.y * 1.15 : 0, ...pts.map((d) => d.y), 1e-9) * 1.1;
  const [x0, x1] = xDomain;
  const sx = (x) => pad.l + ((x - x0) / Math.max(1e-9, x1 - x0)) * iw;
  const sy = (y) => pad.t + ih - ((Math.min(y, top) - yMin) / (top - yMin)) * ih;

  // split into runs so null values create gaps
  const runs = [];
  let cur = [];
  for (const d of data) {
    if (d.y == null || d.x < x0 || d.x > x1) { if (cur.length) runs.push(cur); cur = []; } else cur.push(d);
  }
  if (cur.length) runs.push(cur);
  const path = (run) => run.map((d, i) => `${i ? 'L' : 'M'}${sx(d.x).toFixed(1)},${sy(d.y).toFixed(1)}`).join('');
  const ticksY = [0, 0.5, 1].map((f) => yMin + (top - yMin) * f);
  const ticksX = [0, 0.25, 0.5, 0.75, 1].map((f) => x0 + (x1 - x0) * f);

  return (
    <div ref={ref} className="chart">
      <svg width={w} height={height} role="img">
        {zones.map((z, i) => (
          <rect key={i} className="zone" x={sx(Math.max(z.from, x0))} y={pad.t} width={Math.max(0, sx(Math.min(z.to, x1)) - sx(Math.max(z.from, x0)))} height={ih} />
        ))}
        {ticksY.map((v, i) => (
          <g key={i}>
            <line className="grid" x1={pad.l} x2={w - pad.r} y1={sy(v)} y2={sy(v)} />
            <text className="tick" x={pad.l - 6} y={sy(v) + 3} textAnchor="end">{fmtY(v)}</text>
          </g>
        ))}
        {ticksX.map((v, i) => (
          <text key={i} className="tick" x={sx(v)} y={height - 6} textAnchor={i === 0 ? 'start' : i === 4 ? 'end' : 'middle'}>{fmtX(v)}</text>
        ))}
        {threshold && threshold.y <= top && (
          <g>
            <line className="threshold" x1={pad.l} x2={w - pad.r} y1={sy(threshold.y)} y2={sy(threshold.y)} />
            <text className="threshold-label" x={w - pad.r - 4} y={sy(threshold.y) - 4} textAnchor="end">{threshold.label}</text>
          </g>
        )}
        {markers.filter((m) => m.x >= x0 && m.x <= x1).map((m, i) => (
          <g key={i} className={`marker ${m.tone || ''}`}>
            <line x1={sx(m.x)} x2={sx(m.x)} y1={pad.t} y2={pad.t + ih} />
            <text x={sx(m.x) + 4} y={pad.t + 10 + (i % 2) * 11}>{m.label}</text>
          </g>
        ))}
        {runs.map((r, i) => (
          <g key={i}>
            <path d={`${path(r)}L${sx(r[r.length - 1].x)},${sy(yMin)}L${sx(r[0].x)},${sy(yMin)}Z`} fill={color} opacity="0.10" />
            <path d={path(r)} fill="none" stroke={color} strokeWidth="2" strokeLinejoin="round" />
          </g>
        ))}
        {cursor != null && cursor >= x0 && cursor <= x1 && <line className="cursor" x1={sx(cursor)} x2={sx(cursor)} y1={pad.t} y2={pad.t + ih} />}
        {!pts.length && <text className="tick" x={w / 2} y={height / 2} textAnchor="middle">{empty}</text>}
      </svg>
    </div>
  );
}
