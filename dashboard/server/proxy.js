// proxy.js - serves the real shop on http://localhost:5051 so it can sit inside the dashboard.
// Why: the shop's session cookie has no SameSite attribute, so browsers refuse to send it in a cross-site frame
// (shop on the VM's IP, dashboard on localhost). Without the cookie the cart is empty and checkout cannot work.
// Through this proxy the shop is on "localhost" too, so add-to-cart -> checkout -> "order placed" behaves normally.
import http from 'node:http';
import { VM, hasVm } from './vm.js';

export const PROXY_PORT = Number(process.env.SHOP_PROXY_PORT || 5051);

export function startShopProxy() {
  if (!hasVm) return null;
  const server = http.createServer((req, res) => {
    const headers = { ...req.headers, host: `${VM.host}:30080` };
    delete headers['accept-encoding'];                 // keep bodies uncompressed and untouched
    const up = http.request({ host: VM.host, port: 30080, method: req.method, path: req.url, headers, timeout: 15000 }, (r) => {
      const h = { ...r.headers };
      delete h['x-frame-options'];
      delete h['content-security-policy'];
      res.writeHead(r.statusCode || 502, h);
      r.pipe(res);
    });
    const fail = (msg) => {
      if (res.headersSent) { res.destroy(); return; }
      res.writeHead(502, { 'content-type': 'text/html' });
      res.end(`<body style="font:16px system-ui;padding:40px;background:#fff3f6;color:#7d1239"><h2>Shop not responding</h2><p>${msg}</p>`);
    };
    up.on('timeout', () => { up.destroy(); fail('The shop did not answer within 15 s.'); });
    up.on('error', (e) => fail(`Could not reach the shop (${e.code || e.message}).`));
    req.pipe(up);
  });
  server.on('error', (e) => console.log(`shop proxy not started: ${e.message}`));
  server.listen(PROXY_PORT, '127.0.0.1', () => console.log(`Shop proxy: http://localhost:${PROXY_PORT}  ->  VM shop`));
  return server;
}
