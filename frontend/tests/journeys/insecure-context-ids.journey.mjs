// Journey: the browser code that makes its own ids, on a plain-HTTP address
// where the browser has no crypto.randomUUID (#1261, the running-app half of
// #1255). Two real producers, nothing about the origin faked:
//
//   pairing-exchange   a browser told it has no sign-in opens /pair from a
//                      one-time link and connects, through the real exchange
//   first-agent-run    the setup wizard's first run, whose WebSocket request
//                      carries the id
//
// The native method is only absent on a real plain-HTTP address, so a loopback
// instance (CI's, where localhost counts as secure) still drives both flows but
// cannot prove the fallback. Prove it with an instance the browser reaches over
// the network, and make the run refuse anything less:
//
//   python -m scripts.devloop up --host 0.0.0.0 --browser-host <this machine's address>
//   node tests/journeys/insecure-context-ids.journey.mjs --require-insecure
//
// The preview guards stay as they are. The pairing code is written straight into
// the throwaway instance's database and the exchange request is relayed to its
// own listener, because the preview cookie may do neither. What the browser does
// (make the id, keep it, send it) is the code under test.
import { execFileSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { join } from 'node:path';
import { runJourney } from './runner.mjs';
import { expect } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const REQUIRE_INSECURE = process.argv.includes('--require-insecure');
const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const DEVICE_ID_KEY = 'shuffle-paired-device-id';

const CREATE_GRANT = String.raw`
import json, os, sys
from pathlib import Path
home = Path(sys.argv[1]).resolve()
assert (home / '.branch-preview').exists() or (home.parent / '.branch-preview').exists()
os.environ['DATA_DIR'] = str(home)
from app.core.config import settings
assert settings.effective_data_dir.resolve() == home
from app.core import pairing
from app.db.base import init_pool
init_pool(min_conn=1, max_conn=2)
result = pairing.create_grant(endpoint_url=sys.argv[2], device_label='Journey phone', purpose='controller')
print(json.dumps({'code': result['code']}))
`;

/** A one-time code on this throwaway instance; the data-dir marker is asserted first. */
function createGrant(status) {
	const local = join(status.checkout, '.venv', 'bin', 'python');
	const python = existsSync(local) ? local : (process.env.PYTHON || 'python3');
	const out = execFileSync(python, ['-c', CREATE_GRANT, status.data_dir, new URL(status.urls.ui).origin], {
		cwd: status.checkout,
		env: { ...process.env, PYTHONPATH: join(status.checkout, 'fluid'), PYTHONDONTWRITEBYTECODE: '1' },
		encoding: 'utf8'
	});
	return JSON.parse(out.trim().split('\n').at(-1)).code;
}

/** Whether this page can prove the fallback, and that it was the only way to get an id. */
async function readOrigin(page, log) {
	const origin = await page.evaluate(() => ({ secure: isSecureContext, native: typeof crypto.randomUUID, href: location.protocol }));
	if (REQUIRE_INSECURE) {
		expect(!origin.secure, 'this run needs a plain-HTTP address the browser treats as insecure; see the top of this file');
		expect(origin.native === 'undefined', `crypto.randomUUID is genuinely absent, got ${origin.native}`);
	}
	log(`${origin.href} secure=${origin.secure} crypto.randomUUID=${origin.native}${origin.secure ? ' (loopback: the flow ran, the fallback was not proven)' : ''}`);
	return origin;
}

/** Past Welcome, the optional cloud and the provider check, to the first run. */
async function reachFirstRun(page) {
	const next = () => page.getByRole('button', { name: 'Continue', exact: true }).last();
	const run = page.getByRole('button', { name: /run/i }).last();
	for (let step = 0; step < 6; step += 1) {
		await page.getByText('Watch an agent answer.').or(next()).first().waitFor({ timeout: 20_000 });
		if (await page.getByText('Watch an agent answer.').isVisible()) break;
		await next().click();
		await page.waitForTimeout(200);
	}
	await page.getByText('Watch an agent answer.').waitFor();
	await run.waitFor({ timeout: 20_000 });
	return run;
}

await runJourney({
	name: 'insecure-context-ids',
	// On a loopback address the built app's service worker would answer
	// auth/status itself, past the page routes that keep /setup open.
	browser: async () => ({ context: { serviceWorkers: 'block' } }),
	cases: [
		{
			name: 'pairing-exchange',
			async setup(ctx) {
				const { page, status } = ctx;
				ctx.state = { code: createGrant(status) };
				// This port serves only a browser holding the preview cookie, and the
				// app reads that cookie as the administrator, who gets no Connect
				// button. This page alone is told nobody is signed in.
				await page.route('**/api/v1/auth/status', async (route) => {
					const response = await route.fetch();
					await route.fulfill({ response, json: { ...(await response.json()), authenticated: false, is_admin: false } });
				});
				// The preview cookie may not write pairing state and has to stay unable
				// to, so the request the browser built crosses to the instance's protected
				// listener here instead of through the preview port. The Set-Cookie is
				// dropped: the page keeps the cookie it needs to go on being served.
				await page.route('**/api/v1/pairing/exchange', async (route) => {
					const answer = await fetch(`http://127.0.0.1:${status.app.protected_port}/api/v1/pairing/exchange`, {
						method: 'POST',
						headers: { 'content-type': 'application/json' },
						body: route.request().postData()
					});
					await route.fulfill({ status: answer.status, contentType: 'application/json', body: await answer.text() });
				});
				// The fragment is how the printed link carries its code.
				await page.goto(new URL(`/pair#token=${encodeURIComponent(ctx.state.code)}`, status.urls.ui).toString());
				await page.getByRole('button', { name: 'Connect device', exact: true }).waitFor({ timeout: 30_000 });
			},
			async act({ page, mark, state }) {
				await mark('connect');
				const connect = page.getByRole('button', { name: 'Connect device', exact: true });
				const answered = page.waitForResponse((r) => r.url().includes('/api/v1/pairing/exchange') && r.request().method() === 'POST');
				await connect.click();
				const response = await answered;
				expect(response.ok(), `the exchange was accepted: HTTP ${response.status()}`);
				state.sent = response.request().postDataJSON();
				state.reply = await response.json();
				await page.waitForURL((url) => url.pathname !== '/pair', { timeout: 30_000 });
			},
			async verify({ page, log, state }) {
				await readOrigin(page, log);
				const stored = await page.evaluate((key) => localStorage.getItem(key), DEVICE_ID_KEY);
				expect(UUID_V4.test(stored || ''), `the browser kept a v4 device id: ${stored}`);
				expect(state.sent.device_id === stored, 'the exchange sent the id the browser kept');
				expect(state.reply.status === 'ok', `the server paired this device: ${JSON.stringify(state.reply).slice(0, 200)}`);
				expect(state.reply.client?.device_id === stored, `the server recorded the browser's id: ${state.reply.client?.device_id}`);
			}
		},
		{
			name: 'first-agent-run',
			async setup(ctx) {
				const { page, status } = ctx;
				const frames = [];
				page.on('websocket', (socket) => {
					if (!socket.url().includes('/ws/chat')) return;
					socket.on('framesent', ({ payload }) => {
						try {
							const frame = JSON.parse(String(payload));
							if (frame.type === 'query') frames.push(frame);
						} catch {
							// A binary frame is not the turn start.
						}
					});
				});
				// The workspace is settled on this instance and the preview cookie may
				// not rewrite it; the wizard only needs to get past that screen.
				await page.route('**/api/v1/workspace/config', (route) => {
					if (route.request().method() !== 'POST') return route.fallback();
					return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(route.request().postDataJSON()) });
				});
				// A finished install sends /setup home, so this page alone is told
				// setup is still open. The run below is the real one.
				await page.route('**/api/v1/auth/status', async (route) => {
					const response = await route.fetch();
					await route.fulfill({ response, json: { ...(await response.json()), onboarding_complete: false } });
				});
				await page.goto(new URL('/setup', status.urls.ui).toString());
				ctx.state = { frames, run: await reachFirstRun(page) };
			},
			async act({ page, mark, state }) {
				await mark('run the first agent');
				await state.run.click();
				const deadline = Date.now() + 30_000;
				while (!state.frames.length && Date.now() < deadline) await page.waitForTimeout(50);
			},
			async verify({ page, log, state }) {
				await readOrigin(page, log);
				expect(state.frames.length === 1, `one turn was started: ${state.frames.length}`);
				const id = state.frames[0].request_id;
				expect(UUID_V4.test(id || ''), `the turn carried a v4 request id: ${id}`);
			}
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
});
