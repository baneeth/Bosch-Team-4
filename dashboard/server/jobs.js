// jobs.js - long-running AI jobs started from the dashboard: "plan", or "plan (if needed) -> campaign -> analyze -> report".
// Output is buffered line by line so the browser can poll `GET /api/jobs/:id?from=N`.
// The AI decides what to break. The dashboard only (a) asks the planner for N different, visible failures, (b) checks the
// plan and makes the planner try again if it repeats a type or picks something shoppers cannot see, (c) starts the campaign.
import { sshStream, agentCmd, ssh, pullResults } from './vm.js';
import { RECIPES, planProblems } from './visible.js';

const jobs = new Map();
let seq = 0;
const MAX_PLAN_ATTEMPTS = 3;

export const activeJob = () => [...jobs.values()].find((j) => j.status === 'running') || null;

function newJob(kind, label, meta) {
  const running = activeJob();
  if (running) throw new Error(`"${running.label}" is still running - abort it or wait`);
  const job = { meta, id: `j${++seq}`, kind, label, status: 'running', code: null, lines: [], runId: null, runIds: [], startedAt: Date.now(), cancelled: false };
  jobs.set(job.id, job);
  return job;
}

const say = (job, text) => job.lines.push(`[dashboard] ${text}`);

/** Run agent commands one after another in one ssh session; resolves with the exit code. */
function runSteps(job, steps) {
  return new Promise((resolve) => {
    const p = sshStream(steps.map((s) => agentCmd(s)).join(' && '));
    job.proc = p;
    let buf = '';
    const feed = (chunk) => {
      buf += chunk.toString().replace(/\x1b\[[0-9;]*m/g, '');
      const parts = buf.split(/\r?\n/);
      buf = parts.pop();
      for (const line of parts) {
        job.lines.push(line);
        const m = line.match(/\brun (\d{8}-\d{6}-[a-z_]+-[\w-]+):/);
        if (m && !job.runIds.includes(m[1])) { job.runIds.push(m[1]); job.runId = m[1]; }
      }
      if (job.lines.length > 4000) job.lines.splice(0, job.lines.length - 4000);
    };
    p.stdout.on('data', feed);
    p.stderr.on('data', feed);
    p.on('error', (e) => { say(job, e.message); resolve(1); });
    p.on('close', (code) => { if (buf) job.lines.push(buf); job.proc = null; resolve(code ?? 1); });
  });
}

async function finish(job, code) {
  job.code = code;
  job.status = job.cancelled ? 'aborted' : code === 0 ? 'completed' : code === 2 ? 'aborted' : 'failed';
  job.endedAt = Date.now();
  try { await pullResults('.'); job.synced = true; }          // bring the evidence back for Replay / Findings
  catch (e) { say(job, `could not copy results: ${e.message}`); }
}

const planCli = (count, feedback) => {
  const ask = `Choose exactly ${count} experiments, to run back to back. ${RECIPES}${feedback ? ` YOUR PREVIOUS PLAN WAS REJECTED: ${feedback}. Fix exactly this.` : ''}`;
  return ['plan', '--max-experiments', String(count), '--focus', ask];
};

async function latestPlan() {
  const out = await ssh('cat ~/chaos-agent/results/plans/$(ls -t ~/chaos-agent/results/plans | head -1)', 20000);
  return JSON.parse(out);
}

/** Planner agent, retried until its plan is made of different, visible failures. Returns the plan id or null. */
async function planUntilGood(job, count) {
  let feedback = '';
  for (let attempt = 1; attempt <= MAX_PLAN_ATTEMPTS; attempt++) {
    if (job.cancelled) return null;
    if (attempt > 1) say(job, `asking the planner again (attempt ${attempt}/${MAX_PLAN_ATTEMPTS})`);
    const code = await runSteps(job, [planCli(count, feedback)]);
    if (code !== 0) { say(job, 'the planner failed'); return null; }
    let plan;
    try { plan = await latestPlan(); } catch (e) { say(job, `could not read the plan: ${e.message}`); return null; }
    const problems = planProblems(plan, count);
    if (!problems.length) {
      say(job, `plan accepted: ${plan.experiments.map((e) => `${e.spec.type} on ${e.spec.target}`).join(' -> ')}`);
      return plan.plan_id;
    }
    say(job, `plan rejected - ${problems.join('; ')}`);
    feedback = problems.join('; ');
  }
  say(job, `the planner could not produce ${count} different visible failures in ${MAX_PLAN_ATTEMPTS} tries - nothing was injected`);
  return null;
}

/** kind "plan": only plan. kind "campaign": use planId if given, else plan first (autopilot), then run, analyze, report. */
export function startAI(kind, { count, planId, noAdapt }) {
  const job = newJob(kind, kind === 'plan' ? 'AI planner' : planId ? 'AI campaign' : 'AI autopilot', { type: kind, count });
  (async () => {
    try {
      let id = planId;
      if (!id) id = await planUntilGood(job, count);
      if (!id) return await finish(job, 1);
      if (kind === 'plan') return await finish(job, 0);
      if (job.cancelled) return await finish(job, 2);
      // The budget equals the number of failures asked for, so adapter follow-ups cannot push it past that.
      const run = ['campaign', '--yes', '--max-experiments', String(count), '--plan', id];
      if (noAdapt) run.push('--no-adapt');
      return await finish(job, await runSteps(job, [run, ['analyze'], ['report']]));
    } catch (e) {
      say(job, e.message);
      return await finish(job, 1);
    }
  })();
  return job;
}

export function getJob(id, from = 0) {
  // 'latest' lets a freshly opened page re-attach to the running (or most recent) job
  const j = id === 'latest' ? (activeJob() || [...jobs.values()].pop()) : jobs.get(id);
  if (!j) return null;
  const { proc, lines, cancelled, ...rest } = j;
  return { ...rest, total: lines.length, lines: lines.slice(from) };
}

/** Kill switch: the agent's own `abort` (removes the chaos CR / stops k6 and any campaign) + stop the job. */
export async function abortAll() {
  const j = activeJob();
  if (j) { j.cancelled = true; if (j.proc && j.meta?.type === 'plan') j.proc.kill(); }
  const out = await ssh(agentCmd(['abort']), 30000);
  return out.replace(/\x1b\[[0-9;]*m/g, '').trim();
}
