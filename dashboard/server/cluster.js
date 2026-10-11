// cluster.js - live cluster view: pods + active chaos objects (kubectl over ssh) and resource usage (Prometheus).
import { ssh, kubeCmd, promUrl, hasVm } from './vm.js';

const SPLIT = '@@SPLIT@@';
let podCache = { at: 0, data: null };
let podInflight = null;

const serviceOf = (pod) => pod.replace(/-[a-z0-9]{8,10}-[a-z0-9]{5}$/, '');

function slimPod(p) {
  const cs = p.status?.containerStatuses ?? [];
  const waiting = cs.map((c) => c.state?.waiting?.reason).find(Boolean);
  const terminated = cs.map((c) => c.state?.terminated?.reason).find(Boolean);
  const lastTerm = cs.map((c) => c.lastState?.terminated?.reason).find(Boolean);
  return {
    name: p.metadata.name,
    service: p.metadata.labels?.app || serviceOf(p.metadata.name),
    phase: p.status?.phase,
    deleting: Boolean(p.metadata.deletionTimestamp),
    ready: cs.length > 0 && cs.every((c) => c.ready),
    restarts: cs.reduce((n, c) => n + (c.restartCount || 0), 0),
    reason: waiting || terminated || null,
    lastTermination: lastTerm || null,
    started: p.status?.startTime || null,
  };
}

async function fetchPods() {
  const out = await ssh(kubeCmd(
    `kubectl -n boutique get pods -o json; echo ${SPLIT}; ` +
    'kubectl -n boutique get podchaos,networkchaos,stresschaos -o json 2>/dev/null || echo \'{"items":[]}\'',
  ), 12000);
  const [a, b] = out.split(SPLIT);
  const pods = (JSON.parse(a).items ?? []).map(slimPod);
  let chaos = [];
  try {
    chaos = (JSON.parse(b || '{"items":[]}').items ?? []).map((c) => ({
      kind: c.kind, name: c.metadata.name, deleting: Boolean(c.metadata.deletionTimestamp),
      action: c.spec?.action || c.spec?.mode || '',
      targets: c.spec?.selector?.labelSelectors || c.spec?.selector?.pods || {},
    }));
  } catch { /* none */ }
  return { at: Date.now(), pods, chaos };
}

/** Pods are cached for 1.5 s and shared between concurrent callers so ssh isn't hammered. */
export async function getPods() {
  if (!hasVm) return { at: Date.now(), pods: [], chaos: [], offline: true };
  if (podCache.data && Date.now() - podCache.at < 1500) return podCache.data;
  if (!podInflight) {
    podInflight = fetchPods()
      .then((d) => { podCache = { at: Date.now(), data: d }; return d; })
      .finally(() => { podInflight = null; });
  }
  return podInflight;
}

async function prom(query) {
  const r = await fetch(`${promUrl}/api/v1/query?query=${encodeURIComponent(query)}`, { signal: AbortSignal.timeout(4000) });
  const j = await r.json();
  return j.data?.result ?? [];
}

const lastGood = {};   // svc -> key -> {v, at}: bridges single missing samples

export async function getMetrics() {
  if (!hasVm) return { services: {}, vmCpu: null, offline: true };
  const sel = 'namespace="boutique",container!="",container!="POD"';
  const [cpu, mem, thr, vm] = await Promise.all([
    prom(`sum by (pod) (rate(container_cpu_usage_seconds_total{${sel}}[45s]))`),
    prom(`sum by (pod) (container_memory_working_set_bytes{${sel}})`),
    prom(`sum by (pod) (rate(container_cpu_cfs_throttled_periods_total{${sel}}[45s])) / sum by (pod) (rate(container_cpu_cfs_periods_total{${sel}}[45s]))`),
    prom('1 - avg(rate(node_cpu_seconds_total{mode="idle"}[45s]))'),
  ]);
  const services = {};
  const put = (rows, key, scale = 1) => {
    for (const r of rows) {
      const svc = serviceOf(r.metric.pod);
      const v = Number(r.value[1]);
      if (Number.isFinite(v)) (services[svc] ||= {})[key] = (services[svc][key] ?? 0) + v * scale;
    }
  };
  put(cpu, 'cpuCores');
  put(mem, 'memMiB', 1 / 1048576);
  put(thr, 'throttle');
  const now = Date.now();
  for (const [svc, vals] of Object.entries(services)) for (const [k, v] of Object.entries(vals)) ((lastGood[svc] ||= {})[k] = { v, at: now });
  for (const [svc, keys] of Object.entries(lastGood)) for (const [k, g] of Object.entries(keys)) {
    if (now - g.at < 20000 && services[svc]?.[k] == null) (services[svc] ||= {})[k] = g.v;
  }
  return { services, vmCpu: vm[0] ? Number(vm[0].value[1]) : null };
}
