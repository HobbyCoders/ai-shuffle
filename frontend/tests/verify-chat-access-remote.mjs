// Real-browser check of private access on a mirrored conversation (#1267).
// The session runs on another device ("Owner PC"); this worker forwards the
// panel's calls there. The check answers this worker's API in the browser and
// drives the owner-side request card, saved connection, one-shot approval and
// revocation, at 1440x900 and 390x844.
//
// Run from frontend/ while the fixture is serving:
//   npx vite dev --config tests/vite.chat-access-remote.config.js --host 127.0.0.1 --port 6600 --strictPort
//   node tests/verify-chat-access-remote.mjs
// Needs browser-mcp's playwright-core (npm ci --ignore-scripts --prefix ../browser-mcp)
// and a Chrome/Chromium binary (CHROMIUM_PATH). CHAT_ACCESS_FIXTURE_URL overrides
// the URL; SCREENSHOT_DIR saves a screenshot per step.
import assert from 'node:assert/strict';
import { join } from 'node:path';
import { createRequire } from 'node:module';
const require = createRequire(new URL('../../browser-mcp/package.json', import.meta.url));
const { chromium } = require('playwright-core');
const browser = await chromium.launch({
	executablePath: process.env.CHROMIUM_PATH || {
		win32: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
		darwin: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
	}[process.platform] || '/usr/bin/chromium',
	headless: true,
	args: ['--no-sandbox', '--disable-gpu'],
});
const url = process.env.CHAT_ACCESS_FIXTURE_URL || 'http://127.0.0.1:6600/tests/chat-access-remote.html';
const shots = process.env.SCREENSHOT_DIR;
const later = () => Math.floor(Date.now() / 1000) + 3600;
let passed = 0;

async function harness(viewport, touch) {
	const name = `${viewport.width}x${viewport.height}`;
	const check = (condition, label, detail) => {
		assert.ok(condition, `[${name}] ${label}${detail === undefined ? '' : `: ${JSON.stringify(detail)}`}`);
		passed++;
	};
	const context = await browser.newContext({ viewport, isMobile: touch, hasTouch: touch, deviceScaleFactor: 1, serviceWorkers: 'block' });
	const grant = { id: 'g1', label: 'NAS', kind: 'machine', target: 'root@10.0.0.2:22', expires_at: later(), writable: true, fingerprint: 'SHA256:' + 'a'.repeat(43) };
	const state = {
		calls: [],
		snapshot: {
			grants: [], actions: [], saved_connections: [{ id: 'c1', label: 'NAS', kind: 'machine', target: grant.target, fingerprint: grant.fingerprint }],
			requests: [{ id: 'r1', kind: 'machine', purpose: 'Check disk usage on the NAS', expires_at: later() }],
			vault_available: true, folder_available: false, restart_revokes: true,
		},
	};
	await context.route((u) => u.pathname.startsWith('/api/'), async (route) => {
		const request = route.request();
		const path = new URL(request.url()).pathname;
		if (path === '/api/v1/sessions/remote-chat')
			return route.fulfill({ json: { id: 'remote-chat', mode: 'chat', is_remote: true, device_name: 'Owner PC' } });
		if (!path.startsWith('/api/v1/chat-access/remote-chat')) return route.abort();
		const call = { method: request.method(), path: path.slice('/api/v1/chat-access/remote-chat'.length), body: request.postDataJSON?.() ?? null };
		if (call.method !== 'GET') state.calls.push(call);
		const s = state.snapshot;
		if (call.method === 'POST' && call.path === '/requests/r1') {
			// The owner grants from its own vault; the model's command then waits for approval.
			state.snapshot = { ...s, requests: [], grants: [grant],
				actions: [{ id: 'a1', grant_id: 'g1', operation: 'command', arguments: { command: 'df -h /mnt/user' }, expires_at: later() }] };
			return route.fulfill({ json: { granted: true, saved: true } });
		}
		if (call.method === 'POST' && call.path === '/actions/a1') {
			state.snapshot = { ...s, actions: [] };
			return route.fulfill({ json: { accepted: true } });
		}
		if (call.method === 'DELETE' && call.path === '/grants/g1') {
			state.snapshot = { ...s, grants: [] };
			return route.fulfill({ json: { revoked: true } });
		}
		return route.fulfill({ json: state.snapshot });
	});
	const page = await context.newPage();
	const errors = [];
	page.on('pageerror', (e) => errors.push(e.message));
	const shot = async (step) => { if (shots) await page.screenshot({ path: join(shots, `chat-access-remote-${name}-${step}.png`), fullPage: true }); };
	const tap = (locator) => (touch ? locator.tap() : locator.click());
	const overflow = () => page.evaluate(() => {
		const dialog = document.querySelector('.pa-pop:not([hidden])');
		const box = dialog?.getBoundingClientRect();
		return { page: document.documentElement.scrollWidth > window.innerWidth, dialog: !!box && (box.left < 0 || box.right > window.innerWidth) };
	});

	await page.goto(url);
	const trigger = page.getByRole('button', { name: /^Private access/ });
	await trigger.waitFor();
	check((await trigger.getAttribute('aria-label')) === 'Private access: 1 waiting for you', 'the owner-side request is pending on the viewer');
	await tap(trigger);
	const dialog = page.getByRole('dialog', { name: 'Private access' });
	await dialog.waitFor();
	const card = dialog.getByRole('region', { name: 'Private access request' });
	check(await card.getByText('Check disk usage on the NAS').isVisible(), 'the request card shows the purpose');
	check((await card.getByLabel('Connection').inputValue()) === 'c1', 'the owner\'s saved connection is offered');
	check(await card.getByText(/Uses the saved sign-in for/).isVisible(), 'no credentials are asked for a saved connection');
	check(await card.getByText(/Sent only to Owner PC through this worker/).isVisible(), 'the card names the device that keeps access');
	check(!(await overflow()).page && !(await overflow()).dialog, 'nothing overflows with the request open', await overflow());
	await shot('request');
	await tap(card.getByRole('button', { name: 'Connect' }));
	const approve = dialog.getByRole('button', { name: 'Approve once' });
	await approve.waitFor();
	check(state.calls[0]?.path === '/requests/r1' && state.calls[0].body?.saved_id === 'c1' && state.calls[0].body?.connection === null,
		'the grant went to this worker as a saved-connection choice', state.calls[0]);
	check(await dialog.getByText('df -h /mnt/user').isVisible(), 'the owner\'s pending command is shown for approval');
	await shot('approval');
	await tap(approve);
	await dialog.getByText('df -h /mnt/user').waitFor({ state: 'detached' });
	check(JSON.stringify(state.calls[1]) === JSON.stringify({ method: 'POST', path: '/actions/a1', body: { allow: true } }),
		'the one-shot approval went to this worker for the owner', state.calls[1]);

	check(await dialog.getByText(/Encrypted on Owner PC/).isVisible(), 'saved connections say where they are stored');
	await tap(dialog.getByRole('button', { name: 'Revoke' }));
	await tap(dialog.locator('.pa-small-btn.danger.solid'));
	await dialog.getByRole('button', { name: 'Revoke' }).waitFor({ state: 'detached' });
	check(state.calls[2]?.method === 'DELETE' && state.calls[2]?.path === '/grants/g1', 'revocation went to this worker for the owner', state.calls[2]);
	check(!(await overflow()).page, 'nothing overflows after revocation', await overflow());
	await shot('revoked');
	check(errors.length === 0, 'no page errors', errors);
	await context.close();
}

try {
	await harness({ width: 1440, height: 900 }, false);
	await harness({ width: 390, height: 844 }, true);
	console.log(`chat-access-remote: ${passed} checks passed`);
} finally {
	await browser.close();
}
