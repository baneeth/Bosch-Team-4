// Labels shared by the views. (Fault injection itself is decided by the AI agents - nothing here launches faults.)
export const TYPE_INFO = {
  pod_kill: { label: 'Pod kill', cls: 'errors' },
  pod_failure: { label: 'Pod failure', cls: 'errors' },
  network_delay: { label: 'Network delay', cls: 'slowdown' },
  network_loss: { label: 'Packet loss', cls: 'slowdown' },
  cpu_stress: { label: 'CPU stress', cls: 'resource' },
  memory_stress: { label: 'Memory stress', cls: 'resource' },
  traffic_spike: { label: 'Traffic spike', cls: 'load' },
};

export const FLOWS = [
  ['home', 'Home'], ['product', 'Product page'], ['add_to_cart', 'Add to cart'], ['view_cart', 'View cart'], ['checkout', 'Checkout'],
];

/** The experiment the agent is running right now, read from its log ("run <id>: <type> on <target> for 30s"). */
export function currentRun(lines) {
  let cur = null;
  for (const l of lines) {
    const m = l.match(/\brun (\d{8}-\d{6}-[a-z_]+-[\w-]+): ([a-z_]+) on ([\w-]+)/);
    if (m) cur = { id: m[1], type: m[2], target: m[3], done: false };
    else if (cur && /^\d\d:\d\d:\d\d (COMPLETED|ABORTED)\b|cleaned up/.test(l.trim())) cur = { ...cur, done: true };
  }
  return cur;
}
