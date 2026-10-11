// visible.js - what the dashboard asks of the AI planner, and how it checks the answer.
// The AI still chooses which failures to run. The dashboard only requires that they are different from each other and that
// a shopper can actually notice each one (a CPU stress inside cartservice, say, is contained by its limit and shows nothing).
// Thresholds come from runs recorded in results/ (see CLAUDE.md "Findings so far").

const POD_VISIBLE = new Set(['cartservice', 'redis-cart', 'productcatalogservice', 'paymentservice', 'checkoutservice', 'currencyservice']);
const DELAY_VISIBLE = new Set(['productcatalogservice', 'cartservice', 'currencyservice', 'checkoutservice', 'shippingservice']);
const LOSS_VISIBLE = new Set(['cartservice', 'productcatalogservice']);
const MEM_VISIBLE = new Set(['cartservice', 'productcatalogservice']);

export const RECIPES =
  'DEMO REQUIREMENT: every failure must be visible to shoppers on the live site, and all failures must be of DIFFERENT types ' +
  '(never two of the same type). Ignore the "prefer gaps" rule for this plan. Recipes that are visible on this shop: ' +
  'pod_kill cartservice 30s; pod_failure redis-cart or paymentservice 30s; network_delay productcatalogservice 300ms 60s; ' +
  'network_loss cartservice 20% 30s; memory_stress cartservice 110MB 30s; cpu_stress frontend 2 workers load 100 60s; ' +
  'traffic_spike frontend 200 users ramp 30 duration 90s. Use different services where possible.';

/** Why this planned experiment would not be visible to shoppers (null = fine). */
function invisible(spec) {
  const { type, target, params = {} } = spec;
  switch (type) {
    case 'pod_kill':
    case 'pod_failure':
      return POD_VISIBLE.has(target) ? null : `${type} on ${target} is invisible to shoppers (pick cartservice, redis-cart, paymentservice, productcatalogservice, checkoutservice or currencyservice)`;
    case 'network_delay':
      return DELAY_VISIBLE.has(target) && (params.latency_ms ?? 0) >= 250 ? null : `network_delay on ${target} with ${params.latency_ms ?? '?'} ms is too small to see (use >= 250 ms on productcatalogservice or cartservice)`;
    case 'network_loss':
      return LOSS_VISIBLE.has(target) && (params.loss_pct ?? 0) >= 20 ? null : `network_loss ${params.loss_pct ?? '?'}% on ${target} is too small to see (use >= 20% on cartservice)`;
    case 'memory_stress':
      return MEM_VISIBLE.has(target) && (params.size_mb ?? 0) >= 100 ? null : `memory_stress ${params.size_mb ?? '?'} MB on ${target} will not hurt users (use >= 100 MB on cartservice)`;
    case 'cpu_stress':
      return target === 'frontend' && (params.workers ?? 0) >= 2 && (params.load ?? 0) >= 100 ? null : `cpu_stress on ${target} is contained by its CPU limit (use frontend, 2 workers, load 100)`;
    case 'traffic_spike':
      return (params.users ?? 0) >= 150 ? null : `traffic_spike with ${params.users ?? '?'} users is too small to see (use >= 150)`;
    default:
      return `unknown type ${type}`;
  }
}

/** Problems that make a plan unfit for the demo: too few, repeated types, or invisible failures. */
export function planProblems(plan, count) {
  const exps = plan?.experiments ?? [];
  const out = [];
  if (exps.length < Math.min(2, count)) out.push(`only ${exps.length} experiments were planned (need ${count})`);
  const types = exps.map((e) => e.spec.type);
  const dup = [...new Set(types.filter((t, i) => types.indexOf(t) !== i))];
  if (dup.length) out.push(`failure types repeated: ${dup.join(', ')} - every experiment must have a different type`);
  for (const e of exps) {
    const why = invisible(e.spec);
    if (why) out.push(why);
  }
  return out;
}
