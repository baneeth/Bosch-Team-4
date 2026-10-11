// dev.js - run the API server and the Vite dev server together (hot reload on http://localhost:5173).
import { spawn } from 'node:child_process';

const run = (cmd, args, name) => {
  const p = spawn(cmd, args, { stdio: 'inherit', shell: process.platform === 'win32' });
  p.on('exit', (c) => { console.log(`[${name}] exited (${c})`); process.exit(c ?? 0); });
  return p;
};
run('node', ['server/index.js'], 'api');
run('npx', ['vite', 'client'], 'vite');
