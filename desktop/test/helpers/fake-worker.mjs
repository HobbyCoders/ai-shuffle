// Stand-in for `python -m app.main` in supervisor tests: answers the health
// routes the desktop probes, reports the attach token hash like the real
// /health, refuses other routes without the listener token, records every
// spawn, and can crash shortly after becoming ready.
import http from 'node:http';
import { createHash } from 'node:crypto';
import { appendFileSync } from 'node:fs';

const port = Number(process.env.PORT);
const token = process.env.SHUFFLE_DESKTOP_ATTACH_TOKEN;
// Like the real worker's local gate: everything but health needs the listener token.
const listener = process.env.SHUFFLE_DESKTOP_LISTENER_TOKEN;
const exitAfterReady = Number(process.env.FAKE_WORKER_EXIT_AFTER_READY || 0);
if (process.env.FAKE_WORKER_LOG) appendFileSync(process.env.FAKE_WORKER_LOG, `${process.pid} ${token ?? '-'} ${listener ?? '-'}\n`);
let exiting = false;
const server = http.createServer((req, res) => {
	res.setHeader('content-type', 'application/json');
	if (req.url === '/health') {
		res.end(JSON.stringify({ status: 'healthy', service: 'ai-shuffle', version: 'test', ...(token ? { attach: createHash('sha256').update(token).digest('hex') } : {}) }));
		return;
	}
	if (req.url === '/health/ready') {
		res.end('{"status":"ready"}');
		if (exitAfterReady && !exiting) {
			exiting = true;
			setTimeout(() => process.exit(3), exitAfterReady);
		}
		return;
	}
	if (listener && req.headers['x-shuffle-desktop-listener'] !== listener) {
		res.statusCode = 403;
		return res.end('{"detail":"Desktop listener token required"}');
	}
	if (req.url === '/api/v1/network/startup-sync' && req.method === 'POST') return res.end('{"status":"synced","peers":0}');
	if (req.url === '/api/v1/owner/handoff' && req.method === 'POST') return res.end('{"path":"/api/v1/owner/handoff?token=one-time","expires_in":60}');
	if (req.url === '/api/v1/internal/desktop/stop-sessions') return res.end('{"stopped":true}');
	if (req.url === '/api/v1/internal/desktop/active-sessions') return res.end('{"active_sessions":0}');
	res.statusCode = 404;
	res.end('{}');
});
server.listen(port, '127.0.0.1');
process.on('SIGTERM', () => process.exit(0));
