// vm.js - everything that touches the lab VM: connection settings, ssh helpers, input validation.
// The dashboard never runs kubectl or the agent locally; it asks the VM over ssh (same path as scripts/run.sh).
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
export const REPO = path.resolve(here, '..', '..');
export const RESULTS = path.join(REPO, 'results');

function readEnvFile(file) {
  const out = {};
  try {
    for (const line of fs.readFileSync(file, 'utf8').split(/\r?\n/)) {
      const m = line.match(/^\s*([A-Z_]+)=(.*)$/);
      if (m) out[m[1]] = m[2].trim().replace(/^["']|["']$/g, '');
    }
  } catch { /* file missing -> offline mode */ }
  return out;
}

const env = { ...readEnvFile(path.join(REPO, 'infra', 'vm.env')), ...process.env };
export const VM = {
  host: env.VM_HOST || '',
  user: env.VM_USER || 'ubuntu',
  key: env.VM_KEY ? path.resolve(REPO, env.VM_KEY) : '',
};
export const hasVm = Boolean(VM.host);
export const shopUrl = hasVm ? `http://${VM.host}:30080` : '';
export const promUrl = hasVm ? `http://${VM.host}:30090` : '';
export const grafanaUrl = hasVm ? `http://${VM.host}:30300` : '';

function sshArgs(remote) {
  const a = ['-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=accept-new', '-o', 'ConnectTimeout=8',
    '-o', 'ServerAliveInterval=10'];
  if (VM.key) a.push('-i', VM.key);
  a.push(`${VM.user}@${VM.host}`, remote);
  return a;
}

const PRE = 'export KUBECONFIG=$HOME/.kube/config; ';
export const q = (s) => `'${String(s).replace(/'/g, `'\\''`)}'`;
export const agentCmd = (args) => `${PRE}cd ~/chaos-agent && .venv/bin/chaos-agent ${args.map(q).join(' ')}`;
export const kubeCmd = (cmd) => `${PRE}${cmd}`;

/** Run a command on the VM and resolve with stdout (rejects on non-zero exit or timeout). */
export function ssh(remote, timeoutMs = 15000) {
  return new Promise((resolve, reject) => {
    if (!hasVm) return reject(new Error('no VM configured (infra/vm.env)'));
    const p = spawn('ssh', sshArgs(remote), { windowsHide: true });
    let out = '', err = '';
    const timer = setTimeout(() => { p.kill(); reject(new Error('ssh timeout')); }, timeoutMs);
    p.stdout.on('data', (d) => { out += d; });
    p.stderr.on('data', (d) => { err += d; });
    p.on('error', (e) => { clearTimeout(timer); reject(e); });
    p.on('close', (code) => {
      clearTimeout(timer);
      code === 0 ? resolve(out) : reject(new Error((err || out || `ssh exit ${code}`).trim().slice(0, 300)));
    });
  });
}

/** Start a long-running remote command; returns the child process (stdout/stderr are streams). */
export function sshStream(remote) {
  return spawn('ssh', sshArgs(remote), { windowsHide: true });
}

/** Copy a remote results sub-folder (relative to ~/chaos-agent/results) into ./results. */
export function pullResults(sub = '.') {
  return new Promise((resolve, reject) => {
    if (!hasVm) return reject(new Error('no VM configured'));
    const dest = sub === '.' ? RESULTS : path.join(RESULTS, sub);
    fs.mkdirSync(dest, { recursive: true });
    const src = sshStream(`tar -C ~/chaos-agent/results/${sub} -czf - .`);
    const dst = spawn('tar', ['-xzf', '-'], { cwd: dest, windowsHide: true });
    src.stdout.pipe(dst.stdin);
    let err = '';
    dst.stderr.on('data', (d) => { err += d; });
    dst.on('close', (code) => (code === 0 ? resolve() : reject(new Error(err.slice(0, 200) || `tar exit ${code}`))));
    src.on('error', reject);
    dst.on('error', reject);
  });
}

/**
 * Windows OpenSSH refuses a private key that other accounts can read ("UNPROTECTED PRIVATE KEY FILE").
 * Keys inside OneDrive / project folders inherit broad permissions, so restrict the key to the current user.
 * Idempotent, local-only, and a no-op on Linux/macOS.
 */
export function ensureKeyPrivate() {
  if (process.platform !== 'win32' || !VM.key || !fs.existsSync(VM.key)) return;
  const who = spawnSync('whoami', ['/user', '/fo', 'csv', '/nh'], { encoding: 'utf8', windowsHide: true });
  const sid = (who.stdout || '').trim().split(',')[1]?.replace(/"/g, '');
  if (!sid) return;
  const base = spawnSync('icacls', [VM.key], { encoding: 'utf8', windowsHide: true }).stdout || '';
  if (!/Authenticated Users|BUILTIN\\Users|Everyone/i.test(base)) return;   // already private
  spawnSync('icacls', [VM.key, '/inheritance:r'], { windowsHide: true });
  spawnSync('icacls', [VM.key, '/grant:r', `*${sid}:(R)`], { windowsHide: true });
  console.log('restricted permissions on the ssh key (current user only)');
}
