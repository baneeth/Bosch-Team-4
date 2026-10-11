// probe.js - the dashboard's own storefront monitor. Two virtual shoppers walk the five user flows against the
// shop's public NodePort and the results are kept as 1-second buckets, so the browser can draw the crash live.
import { shopUrl } from './vm.js';

const FLOWS = ['home', 'product', 'add_to_cart', 'view_cart', 'checkout'];
const PRODUCTS = ['OLJCESPC7Z', '66VCHSJNUP', '1YMWWN1N4O', 'L9ECAV7KIM', '2ZYFJ3GM2N', '0PUK6V6EV0'];
const CHECKOUT = {
  email: 'demo@example.com', street_address: '1600 Amphitheatre Parkway', zip_code: '94043', city: 'Mountain View',
  state: 'CA', country: 'United States', credit_card_number: '4432801561520454',
  credit_card_expiration_month: '1', credit_card_expiration_year: '2039', credit_card_cvv: '672',
};
const TIMEOUT_MS = 5000;     // same as the agent's probe: slower than this counts as an error
const KEEP_S = 900;          // history kept in memory
const USERS = 2;

const samples = [];          // {t (ms), flow, ok, status, ms}
let running = false;

function percentile(sorted, q) {
  if (!sorted.length) return null;
  const i = Math.min(sorted.length - 1, Math.ceil(q * sorted.length) - 1);
  return Math.round(sorted[Math.max(0, i)] * 10) / 10;
}

async function hit(jar, flow, product) {
  const url = shopUrl;
  const t0 = performance.now();
  let status = 0, ok = false;
  try {
    const init = { redirect: 'follow', signal: AbortSignal.timeout(TIMEOUT_MS), headers: jar.cookie ? { cookie: jar.cookie } : {} };
    let path = '/';
    if (flow === 'product') path = `/product/${product}`;
    if (flow === 'view_cart') path = '/cart';
    if (flow === 'add_to_cart' || flow === 'checkout') {
      init.method = 'POST';
      init.headers['content-type'] = 'application/x-www-form-urlencoded';
      init.body = new URLSearchParams(flow === 'checkout' ? CHECKOUT : { product_id: product, quantity: '1' }).toString();
      path = flow === 'checkout' ? '/cart/checkout' : '/cart';
    }
    const res = await fetch(url + path, init);
    const sc = res.headers.getSetCookie?.() ?? [];
    if (sc.length) jar.cookie = sc.map((c) => c.split(';')[0]).join('; ');
    await res.arrayBuffer();
    status = res.status;
    ok = res.status < 400;
  } catch {
    status = 0;          // timeout / refused / reset: the shop is not answering
  }
  samples.push({ t: Date.now(), flow, ok, status, ms: performance.now() - t0 });
}

async function shopper(idx) {
  const jar = {};
  await new Promise((r) => setTimeout(r, idx * 450));
  for (;;) {
    const product = PRODUCTS[Math.floor(Math.random() * PRODUCTS.length)];
    for (const flow of FLOWS) {
      await hit(jar, flow, product);
      await new Promise((r) => setTimeout(r, 500));
    }
  }
}

export function startProbe() {
  if (running || !shopUrl) return;
  running = true;
  for (let i = 0; i < USERS; i++) shopper(i);
  setInterval(() => {                       // drop old samples
    const cut = Date.now() - KEEP_S * 1000;
    while (samples.length && samples[0].t < cut) samples.shift();
  }, 5000).unref();
}

function stats(list) {
  const lat = list.map((s) => s.ms).sort((a, b) => a - b);
  const errors = list.filter((s) => !s.ok).length;
  return { n: list.length, errors, errorRate: list.length ? errors / list.length : 0, p50: percentile(lat, 0.5), p95: percentile(lat, 0.95) };
}

/** Snapshot for the UI: rolling 5 s window, per-flow state, and a 1 s series since `since` (ms epoch). */
export function snapshot(since = 0) {
  const now = Date.now();
  const win = samples.filter((s) => s.t > now - 5000);
  const flows = {};
  for (const f of FLOWS) {
    const fl = win.filter((s) => s.flow === f);
    const last = [...samples].reverse().find((s) => s.flow === f);
    flows[f] = { ...stats(fl), lastStatus: last?.status ?? null, lastMs: last ? Math.round(last.ms) : null, lastOk: last?.ok ?? null };
  }
  const overall = stats(win);
  const lastOk = [...samples].reverse().find((s) => s.ok);
  const silentS = lastOk ? (now - lastOk.t) / 1000 : null;
  let state = 'unknown';
  if (samples.length >= 3) {
    if (overall.n === 0 || overall.errorRate >= 0.5 || (silentS != null && silentS > 6)) state = 'down';
    else if (overall.errorRate > 0.05 || (overall.p95 ?? 0) > 800) state = 'degraded';
    else state = 'healthy';
  }
  const series = [];
  const from = Math.max(since, now - KEEP_S * 1000);
  for (let t = Math.floor(from / 1000) * 1000; t < now - 1000; t += 1000) {
    const b = samples.filter((s) => s.t >= t && s.t < t + 1000);
    // for latency use a trailing 3 s window so single slow requests stay visible
    const w = samples.filter((s) => s.t >= t - 2000 && s.t < t + 1000);
    const st = stats(w);
    series.push({ t, n: b.length, errors: b.filter((s) => !s.ok).length, errorRate: stats(b).errorRate, p95: st.p95, p50: st.p50 });
  }
  return { now, state, overall, flows, silentS, series, shopUrl };
}
