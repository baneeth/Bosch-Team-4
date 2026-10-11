// Chaos Lab dashboard server - dependency-free Node. Serves the built React app and a small JSON API.
//   node server/index.js        (PORT defaults to 5050, bound to localhost only)
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { hasVm, shopUrl, grafanaUrl, promUrl, pullResults, ensureKeyPrivate } from './vm.js';
import { startProbe, snapshot } from './probe.js';
import { getPods, getMetrics } from './cluster.js';
import * as results from './results.js';
import { startAI, getJob, activeJob, abortAll } from './jobs.js';
import { startShopProxy, PROXY_PORT } from './proxy.js';

const here = path.dirname(fileURLToPath(import.meta.url));
const DIST = path.resolve(here, '..', 'client', 'dist');
const PORT = Number(process.env.PORT || 5050);

const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.json': 'application/json', '.ico': 'image/x-icon', '.woff2': 'font/woff2' };

const send = (res, code, body) => {
  const text = JSON.stringify(body);
  res.writeHead(code, { 'content-type': 'application/json', 'cache-control': 'no-store' });
  res.end(text);
};
const readBody = (req) => new Promise((resolve) => {
  let b = '';
  req.on('data', (d) => { b += d; if (b.length > 1e5) req.destroy(); });
  req.on('end', () => { try { resolve(JSON.parse(b || '{}')); } catch { resolve({}); } });
});

// 2-4 failures, chosen and ordered by the AI planner
const failureCount = (body) => Math.min(4, Math.max(2, Math.round(Number(body?.count)) || 3));

async function api(req, res, url) {
  const p = url.pathname;
  const m = req.method;
  try {
    if (p === '/api/config') {
      return send(res, 200, { vm: hasVm, shopUrl, proxyUrl: hasVm ? `http://localhost:${PROXY_PORT}` : '', grafanaUrl, promUrl, activeJob: activeJob()?.id ?? null });
    }
    if (p === '/api/health') return send(res, 200, snapshot(Number(url.searchParams.get('since')) || 0));
    if (p === '/api/pods') return send(res, 200, await getPods());
    if (p === '/api/metrics') return send(res, 200, await getMetrics());

    if (p === '/api/graph') return send(res, 200, results.getGraph() ?? {});
    if (p === '/api/baseline') return send(res, 200, results.getBaseline() ?? {});
    if (p === '/api/runs') return send(res, 200, results.listRuns());
    if (p.startsWith('/api/runs/')) {
      const r = results.getRun(decodeURIComponent(p.slice(10)));
      return r ? send(res, 200, r) : send(res, 404, { error: 'run not found' });
    }
    if (p === '/api/campaigns') return send(res, 200, results.listCampaigns());
    if (p.startsWith('/api/campaigns/')) {
      const c = results.getCampaign(decodeURIComponent(p.slice(15)));
      return c ? send(res, 200, c) : send(res, 404, { error: 'campaign not found' });
    }
    if (p === '/api/reports') return send(res, 200, results.listReports());
    if (p.startsWith('/api/reports/')) {
      const r = results.getReport(decodeURIComponent(p.slice(13)));
      return r ? send(res, 200, r) : send(res, 404, { error: 'report not found' });
    }

    // The AI decides what to break: the planner picks the experiments, the campaign injects them under the
    // deterministic guard, the analyst scores the result. The dashboard only starts those agent commands.
    if (p === '/api/plan' && m === 'POST') {
      const body = await readBody(req);
      return send(res, 200, { id: startAI('plan', { count: failureCount(body) }).id });
    }
    if (p === '/api/campaign' && m === 'POST') {
      const body = await readBody(req);
      if (body.planId && !/^[\w.-]+$/.test(body.planId)) throw new Error('bad plan id');
      const job = startAI('campaign', { count: failureCount(body), planId: body.planId, noAdapt: Boolean(body.noAdapt) });
      return send(res, 200, { id: job.id });
    }
    if (p === '/api/plans') return send(res, 200, results.listPlans());
    if (p.startsWith('/api/plans/')) {
      const x = results.getPlan(decodeURIComponent(p.slice(11)));
      return x ? send(res, 200, x) : send(res, 404, { error: 'plan not found' });
    }
    if (p.startsWith('/api/jobs/')) {
      const j = getJob(p.slice(10), Number(url.searchParams.get('from')) || 0);
      return j ? send(res, 200, j) : send(res, 404, { error: 'job not found' });
    }
    if (p === '/api/abort' && m === 'POST') return send(res, 200, { output: await abortAll() });
    if (p === '/api/sync' && m === 'POST') { await pullResults('.'); return send(res, 200, { ok: true }); }

    return send(res, 404, { error: 'unknown endpoint' });
  } catch (e) {
    return send(res, 500, { error: e.message });
  }
}

function serveStatic(req, res, url) {
  let file = path.join(DIST, decodeURIComponent(url.pathname));
  if (!file.startsWith(DIST)) { res.writeHead(403); return res.end(); }
  if (!fs.existsSync(file) || fs.statSync(file).isDirectory()) file = path.join(DIST, 'index.html');
  if (!fs.existsSync(file)) {
    res.writeHead(200, { 'content-type': 'text/plain' });
    return res.end('Dashboard not built yet - run "npm run build" in the dashboard folder (or "npm run dev").');
  }
  res.writeHead(200, { 'content-type': MIME[path.extname(file)] || 'application/octet-stream' });
  fs.createReadStream(file).pipe(res);
}

http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost');
  return url.pathname.startsWith('/api/') ? api(req, res, url) : serveStatic(req, res, url);
}).listen(PORT, '127.0.0.1', () => {
  console.log(`Chaos Lab dashboard: http://localhost:${PORT}   (VM ${hasVm ? 'configured' : 'NOT configured - replay mode only'})`);
  ensureKeyPrivate();
  startShopProxy();
  startProbe();
});
