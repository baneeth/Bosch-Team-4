import React, { useEffect, useState } from 'react';
import { FLOWS } from '../meta.js';
import { ms, pct } from '../api.js';

const STATE_TEXT = {
  healthy: ['Shop is healthy', 'Every user flow answers quickly.'],
  degraded: ['Shop is degraded', 'Users are seeing slow pages or some errors.'],
  down: ['Shop is DOWN', 'The storefront is not answering shoppers.'],
  unknown: ['Waiting for the first probes', 'Two virtual shoppers walk the five flows continuously.'],
};

// Quick links into the real shop (the frame is fully interactive: browse, add to cart, check out, place an order).
const PAGES = [
  { id: 'home', label: 'Home', path: '/' },
  { id: 'product', label: 'Product page', path: '/product/OLJCESPC7Z' },
  { id: 'cart', label: 'Cart', path: '/cart' },
];

function flowTone(f) {
  if (!f || f.n === 0) return f?.lastOk === false ? 'bad' : 'unknown';
  if (f.errorRate >= 0.5) return 'bad';
  if (f.errorRate > 0.05 || (f.p95 ?? 0) > 800) return 'warn';
  return 'ok';
}

const flowWhat = (f) => (f?.lastStatus === 0 ? 'timeout' : f?.lastStatus ? `HTTP ${f.lastStatus}` : 'no reply');

/** The real shop, served through the dashboard's proxy so cookies work: the cart, checkout and "order placed" all function. */
function ShopFrame({ frameUrl, failing, slow, state }) {
  const [src, setSrc] = useState('/');
  const [nonce, setNonce] = useState(0);
  const tone = state === 'down' || failing.length ? 'bad' : slow.length ? 'warn' : 'ok';
  return (
    <div className="shopview">
      <div className="shop-bar">
        <span className="shop-title">Live shop</span>
        {PAGES.map((p) => (
          <button key={p.id} className={`page-tab ${src === p.path ? 'on' : ''}`} onClick={() => { setSrc(p.path); setNonce((n) => n + 1); }}>{p.label}</button>
        ))}
        <span className="shop-hint">Real site - add items, check out and place an order while the AI injects faults.</span>
      </div>
      <div className={`frame-wrap ${tone}`}>
        <iframe key={`${src}-${nonce}`} title="Online Boutique" src={frameUrl + src} />
        {tone === 'bad' && (
          <div className="frame-alert bad">
            <b>{state === 'down' ? 'Shop not responding' : 'Shoppers are hitting errors'}</b>
            <span>{state === 'down' ? 'No page is being served.' : `Failing now: ${failing.join(', ')} - open those pages to see the real error`}</span>
          </div>
        )}
        {tone === 'warn' && <div className="frame-alert warn"><b>Pages are slow</b><span>{slow.join(', ')} - shoppers wait</span></div>}
      </div>
    </div>
  );
}

export default function Storefront({ snap, offline, shopUrl, frameUrl }) {
  const [frame, setFrame] = useState(true);
  const state = offline ? 'unknown' : snap?.state ?? 'unknown';
  const [title, sub] = STATE_TEXT[state];
  const o = snap?.overall;
  const failing = FLOWS.filter(([id]) => flowTone(snap?.flows?.[id]) === 'bad');
  const slow = FLOWS.filter(([id]) => flowTone(snap?.flows?.[id]) === 'warn');
  return (
    <div className={`card storefront ${state}`}>
      <div className="card-head">
        <h2>Online Boutique - live storefront</h2>
        {shopUrl && <button className="btn ghost" onClick={() => setFrame((f) => !f)}>{frame ? 'Hide' : 'Show'} the real site</button>}
      </div>

      <div className="status-row">
        <div className={`beacon ${state}`}><span /></div>
        <div className="status-text">
          <strong>{offline ? 'Dashboard server unreachable' : title}</strong>
          <span>{offline ? 'Start it with "npm start" in the dashboard folder.' : sub}</span>
        </div>
        <dl className="kpis">
          <div><dt>Error rate</dt><dd className={o?.errorRate > 0.05 ? 'bad' : ''}>{pct(o?.errorRate, 1)}</dd></div>
          <div><dt>p95 latency</dt><dd className={(o?.p95 ?? 0) > 800 ? 'warn' : ''}>{ms(o?.p95)}</dd></div>
          <div><dt>Median</dt><dd>{ms(o?.p50)}</dd></div>
          <div><dt>Last good reply</dt><dd className={snap?.silentS > 3 ? 'bad' : ''}>{snap?.silentS == null ? '-' : `${snap.silentS.toFixed(1)} s ago`}</dd></div>
        </dl>
      </div>

      {(failing.length > 0 || slow.length > 0) && (
        <div className="impact-line">
          {failing.length > 0 && <span className="bad"><b>Failing for shoppers:</b> {failing.map(([id, l]) => `${l} (${flowWhat(snap.flows[id])})`).join(' · ')}</span>}
          {slow.length > 0 && <span className="warn"><b>Slow:</b> {slow.map(([, l]) => l).join(' · ')}</span>}
        </div>
      )}

      <div className="flows">
        {FLOWS.map(([id, label]) => {
          const f = snap?.flows?.[id];
          const tone = flowTone(f);
          return (
            <div key={id} className={`flow ${tone}`}>
              <span className="flow-name">{label}</span>
              <span className="flow-val">{tone === 'bad' ? flowWhat(f) : f?.lastStatus ? `HTTP ${f.lastStatus}` : '-'}</span>
              <span className="flow-sub">{ms(f?.p95)} p95 · {pct(f?.errorRate)} err</span>
            </div>
          );
        })}
      </div>

      {frame && (frameUrl || shopUrl) && <ShopFrame frameUrl={frameUrl || shopUrl} state={state} failing={failing.map(([, l]) => l)} slow={slow.map(([, l]) => l)} />}
    </div>
  );
}
