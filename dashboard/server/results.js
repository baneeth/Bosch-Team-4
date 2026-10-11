// results.js - read-only access to ./results (runs, campaigns, plans, reports) written by the agent.
import fs from 'node:fs';
import path from 'node:path';
import { RESULTS } from './vm.js';

const safeId = (s) => /^[\w.-]+$/.test(s) && !s.includes('..');
const readJson = (f) => { try { return JSON.parse(fs.readFileSync(f, 'utf8')); } catch { return null; } };
const dirs = (sub) => {
  try {
    return fs.readdirSync(path.join(RESULTS, sub), { withFileTypes: true }).filter((d) => d.isDirectory()).map((d) => d.name).sort();
  } catch { return []; }
};

function parseCsv(text) {
  const [head, ...rows] = text.trim().split(/\r?\n/);
  const cols = head.split(',');
  return rows.map((r) => {
    const v = r.split(',');
    const o = {};
    cols.forEach((c, i) => { o[c] = v[i] === '' || v[i] === undefined ? null : (isNaN(Number(v[i])) ? v[i] : Number(v[i])); });
    return o;
  });
}

export function listRuns() {
  return dirs('runs').map((id) => {
    const r = readJson(path.join(RESULTS, 'runs', id, 'result.json'));
    if (!r) return null;
    return {
      id, type: r.spec?.type, target: r.spec?.target, params: r.spec?.params, duration_s: r.spec?.duration_s,
      status: r.status, abort_reason: r.abort_reason, started_at: r.started_at, recovery_s: r.recovery_s,
      pod_ready_s: r.pod_ready_s, peak_error_rate: r.peak_error_rate, peak_p95_ms: r.peak_p95_ms, worst_flow: r.worst_flow,
    };
  }).filter(Boolean).reverse();
}

export function getRun(id) {
  if (!safeId(id)) return null;
  const base = path.join(RESULTS, 'runs', id);
  const result = readJson(path.join(base, 'result.json'));
  if (!result) return null;
  let timeline = [], events = [];
  try { timeline = parseCsv(fs.readFileSync(path.join(base, 'timeline.csv'), 'utf8')); } catch { /* optional */ }
  try { events = fs.readFileSync(path.join(base, 'events.log'), 'utf8').trim().split(/\r?\n/); } catch { /* optional */ }
  return { result, timeline, events };
}

export function getGraph() { return readJson(path.join(RESULTS, 'graph.json')); }
export function getBaseline() { return readJson(path.join(RESULTS, 'baseline.json')); }

export function listCampaigns() {
  return dirs('campaigns').map((id) => {
    const c = readJson(path.join(RESULTS, 'campaigns', id, 'campaign.json'));
    return c ? { id, status: c.status, started_at: c.started_at, ended_at: c.ended_at, runs: (c.entries ?? []).length } : null;
  }).filter(Boolean).reverse();
}

export function getCampaign(id) {
  if (!safeId(id)) return null;
  const base = path.join(RESULTS, 'campaigns', id);
  const campaign = readJson(path.join(base, 'campaign.json'));
  if (!campaign) return null;
  return { campaign, plan: readJson(path.join(base, 'plan.json')) };
}

export function listReports() {
  return dirs('reports').map((id) => {
    const a = readJson(path.join(RESULTS, 'reports', id, 'analysis.json'));
    return a ? { id, created_at: a.created_at, overall_score: a.overall_score, grade: a.grade, scope: a.scope, runs: a.run_ids?.length } : null;
  }).filter(Boolean).reverse();
}

export function getReport(id) {
  if (!safeId(id)) return null;
  const base = path.join(RESULTS, 'reports', id);
  const analysis = readJson(path.join(base, 'analysis.json'));
  if (!analysis) return null;
  let markdown = '';
  try { markdown = fs.readFileSync(path.join(base, 'findings-report.md'), 'utf8'); } catch { /* optional */ }
  return { analysis, markdown };
}

const planFiles = () => {
  try { return fs.readdirSync(path.join(RESULTS, 'plans')).filter((f) => f.endsWith('.json')).map((f) => f.slice(0, -5)).sort(); } catch { return []; }
};
export function listPlans() {
  return planFiles().map((id) => {
    const p = readJson(path.join(RESULTS, 'plans', id + '.json'));
    return p ? { id, created_at: p.created_at, source: p.source, model: p.model, experiments: (p.experiments ?? []).length } : null;
  }).filter(Boolean).reverse();
}
export function getPlan(id) {
  if (!safeId(id)) return null;
  return readJson(path.join(RESULTS, 'plans', id + '.json'));
}
