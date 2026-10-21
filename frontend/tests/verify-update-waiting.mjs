// Real-browser check of the restart waiting state and install outcomes (#344):
// refused, partial, complete, failed and waiting operations; blocked by a
// service, still busy after a stop, waiting for idle, stopping, queued; a
// retained download behind a newer offer or on an idle updater; a staged
// worker held by one that must check first (#361); and a not-sent action
// result. The update API is answered here, per scenario.
// UPDATE_WAITING_ONLY=name,name runs just those scenarios.
//
// Run from frontend/ while the fixture is serving:
//   npx vite dev --config tests/vite.update-button.config.js --host 127.0.0.1 --port 5248 --strictPort
//   SCREENSHOT_DIR=/tmp/update-waiting node tests/verify-update-waiting.mjs
//
// Or inside the real app shell (sidebar, phone drawer, anchored dock), with
// every API call answered here and the dev proxy pointed at a dead port so no
// request can reach a running worker:
//   AI_SHUFFLE_DEV_API=http://127.0.0.1:9 npx vite dev --host 127.0.0.1 --port 5249 --strictPort
//   UPDATE_WAITING_SHELL_URL=http://127.0.0.1:5249/ SCREENSHOT_DIR=/tmp/update-waiting node tests/verify-update-waiting.mjs
//
// Needs playwright-core (browser-mcp's, or PLAYWRIGHT_MODULE) and Chromium
// (CHROMIUM_PATH). UPDATE_WAITING_FIXTURE_URL overrides the URL.
import assert from 'node:assert/strict';
import { join } from 'node:path';
import { createRequire } from 'node:module';
const require = createRequire(new URL('../../browser-mcp/package.json', import.meta.url));
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright-core');
const browser = await chromium.launch({
	executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium',
	headless: true,
	args: ['--no-sandbox', '--disable-gpu'],
});
const shell = process.env.UPDATE_WAITING_SHELL_URL;
const url = shell || process.env.UPDATE_WAITING_FIXTURE_URL || 'http://127.0.0.1:5248/tests/update-waiting.html';
// The app shell's own boot reads: a signed-in local owner; everything else is empty.
const AUTH = { authenticated: true, is_admin: true, setup_required: false, auth_mode: 'local', local_access: true, claude_authenticated: true,
	has_any_credential: true, github_authenticated: false, onboarding_complete: true, username: 'owner', agent_gate: { ready: true } };
const shots = process.env.SCREENSHOT_DIR;

const now = Date.now() / 1000;
const iso = (secondsAgo) => new Date((now - secondsAgo) * 1000).toISOString();
const self = { device_id: 'local', device_name: 'Current worker', is_self: true, online: true, supported: true, allow_updates: true,
	version: 'c'.repeat(40), running_version: 'b'.repeat(40), staged_version: 'c'.repeat(40), monitor_serving_idle_seconds: 1800 };
const docker = { ...self, device_id: 'docker', device_name: 'Linux Docker worker', is_self: false };
const desktop = { ...self, device_id: 'desk', device_name: 'Windows desktop worker', is_self: false };
const service = { session_id: 's1', title: 'Fix login page', kind: 'service', task_id: 't1', label: 'npm run preview -- --port 5173', port: 5173,
	started_at: iso(3 * 3600), since: iso(3 * 3600), live: true, liveness: 'live', running_turn: false, turn_ended_at: iso(2.6 * 3600),
	stop_will_stop: true, monitor_serving_idle_seconds: 1800, idle_stop_at: now + 1500 };
const turn = { session_id: 's2', title: 'Write release notes', kind: 'turn', since: iso(240), live: true, liveness: 'live', running_turn: true };
const one = (workers, extra = {}) => ({ allow_updates: true, update_all_workers: false, server_time: now, workers, ...extra });
const all = (workers, extra = {}) => ({ ...one(workers, extra), update_all_workers: true });
const op = (outcome, phase, workers, refused = [], extra = {}) => ({ id: `op-${outcome}`, phase, outcome, refused_workers: refused, workers, ...extra });

// [name, snapshot, interaction, expected text, POST reply]
const ONLY = process.env.UPDATE_WAITING_ONLY?.split(',');
const SCENARIOS = [
	['refused', one([{ ...self, phase: 'ready', install_state: 'blocked', wait_reason: 'running_work', blocked_by: [turn] }], {
		operation: op('refused', 'error', [{ device_id: 'local', device_name: 'Current worker', status: 'skipped', message: 'Busy with running work: Write release notes' }],
			[{ device_id: 'local', device_name: 'Current worker', reason: 'Busy with running work: Write release notes' }]) }),
	'hover', 'Restart was not started: Current worker: Busy with running work: Write release notes.'],
	['partial', all([{ ...self, phase: 'idle', version: null, staged_version: null, install_state: 'idle' }, { ...docker, phase: 'idle', version: null, staged_version: null, running_version: 'c'.repeat(40), install_state: 'idle' }], {
		operation: op('partial', 'complete', [
			{ device_id: 'docker', device_name: 'Linux Docker worker', status: 'verified', message: 'Updated and verified' },
			{ device_id: 'local', device_name: 'Current worker', status: 'skipped', message: 'Busy with running work: Fix login page' }],
			[{ device_id: 'local', device_name: 'Current worker', reason: 'Busy with running work: Fix login page' }], { completed_at: now - 20 }) }),
	'click', 'Not restarted: Current worker Busy with running work: Fix login page'],
	['complete', all([{ ...self, phase: 'idle', version: null, staged_version: null, install_state: 'idle' }], {
		operation: op('complete', 'complete', [
			{ device_id: 'local', device_name: 'Current worker', status: 'verified', message: 'Updated and verified' },
			{ device_id: 'docker', device_name: 'Linux Docker worker', status: 'verified', message: 'Updated and verified' }], [], { completed_at: now - 5 }) }),
	'celebrate', null],
	['failed', all([{ ...self, phase: 'ready', install_state: 'ready', blocked_by: [] }, { ...desktop, phase: 'ready', install_state: 'ready', blocked_by: [] }], {
		operation: op('failed', 'error', [
			{ device_id: 'local', device_name: 'Current worker', status: 'verified', message: 'Updated and verified' },
			{ device_id: 'desk', device_name: 'Windows desktop worker', status: 'error', message: 'Startup not confirmed. Check this worker before retrying.' }]) }),
	'hover', 'Windows desktop worker: Startup not confirmed. Check this worker before retrying.'],
	['waiting', all([{ ...self, phase: 'idle', install_state: 'queued', wait_reason: 'updater_acknowledgement', blocked_by: [] }], {
		operation: op('waiting', 'waiting', [
			{ device_id: 'docker', device_name: 'Linux Docker worker', status: 'waiting', message: 'Verifying updated version…' },
			{ device_id: 'local', device_name: 'Current worker', status: 'skipped', message: 'Busy with running work: Fix login page' }],
			[{ device_id: 'local', device_name: 'Current worker', reason: 'Busy with running work: Fix login page' }]) }),
	'hover', 'Not restarted: Current worker: Busy with running work: Fix login page.'],
	['blocked-service', one([{ ...self, phase: 'ready', install_state: 'blocked', wait_reason: 'running_work', blocked_by: [service, turn] }]),
		'click', 'Service npm run preview -- --port 5173 still running for Fix login page on port 5173'],
	['still-busy-after-stop', one([{ ...self, phase: 'ready', install_state: 'blocked', wait_reason: 'still_busy_after_stop', blocked_by: [service],
		stop_result: { request_id: 'r1', accepted: false, reason: 'Busy with running work: Preview server still running for Fix login page', completed_at: now - 30 } }]),
		'click', 'The stop attempt finished, but work is still running. Restart was not queued.'],
	['waiting-for-idle', one([{ ...self, phase: 'ready', install_state: 'waiting_for_idle', wait_reason: 'running_work', deferred: true, blocked_by: [service],
		restart_expires_at: now + 11.5 * 3600 }]), 'click', 'Will restart when running work finishes.'],
	['waiting-for-idle-no-estimate', one([{ ...self, phase: 'ready', install_state: 'waiting_for_idle', wait_reason: 'running_work', deferred: true, monitor_serving_idle_seconds: 0,
		blocked_by: [{ ...service, monitor_serving_idle_seconds: 0, idle_stop_at: null }], restart_expires_at: now + 11.5 * 3600 }]), 'click', 'No known automatic stop time.'],
	['stopping', one([{ ...self, phase: 'ready', install_state: 'stopping', wait_reason: 'stopping_work', blocked_by: [service] }]),
		'click', 'Stop requested. Stopping running work, then requesting restart…'],
	['queued', one([{ ...self, phase: 'ready', install_state: 'queued', wait_reason: 'updater_acknowledgement', blocked_by: [] }]),
		'click', 'Restart requested; waiting for the updater…'],
	['newer-offer', one([{ ...self, phase: 'available', version: 'd'.repeat(40), staged_version: 'c'.repeat(40), install_state: 'unavailable', wait_reason: 'newer_offer',
		release: '1.4.0', notes: '- Faster startup\n- Restart waiting details', role: 'available', label: 'Update available; not downloaded' }]),
		'hover', 'Downloaded cccccccccccc is retained. A newer update is available; download it to continue.'],
	['updater-idle', one([{ ...self, phase: 'idle', version: null, install_state: 'unavailable', wait_reason: 'updater_idle', role: 'available', label: 'Check for updates before restarting' }]),
		'hover', 'Previous download retained; the updater is idle. Click to check for updates.'],
	// One staged worker and one that must check first (#361): the click checks, never installs.
	['mixed-check-first', all([{ ...self, phase: 'ready', install_state: 'ready', blocked_by: [], role: 'staged', label: 'Staged, awaiting restart' },
		{ ...desktop, phase: 'idle', version: null, install_state: 'unavailable', wait_reason: 'updater_idle', role: 'available', label: 'Check for updates before restarting' }],
		{ plan: { action: 'check', ready: false, held: 'Check for updates needed on Windows desktop worker; nothing was installed' } }),
		'check-first', 'All online workers: Restart waits for an update check on Windows desktop worker. Click to check for updates.'],
	['mixed-check-first-blocked', all([{ ...self, phase: 'ready', install_state: 'blocked', wait_reason: 'running_work', blocked_by: [service], role: 'staged', label: 'Staged, awaiting restart' },
		{ ...desktop, phase: 'idle', version: null, install_state: 'unavailable', wait_reason: 'updater_idle', role: 'available', label: 'Check for updates before restarting' }],
		{ plan: { action: 'check', ready: false, held: 'Check for updates needed on Windows desktop worker; nothing was installed' } }),
		'click', 'Restart waits for an update check on Windows desktop worker. Restart controls return once every worker is staged.'],
	['not-sent', one([{ ...self, phase: 'ready', install_state: 'blocked', wait_reason: 'running_work', blocked_by: [service] }]),
		'not-sent', 'Last request: Not sent to Current worker: Waiting on workers to finish the previous update.',
		{ scope: 'current', sent: null, action: 'install', mode: 'when_idle', request_state: 'not_sent', results: [
			{ device_id: 'local', device_name: 'Current worker', accepted: false, request_state: 'not_sent', mode: 'when_idle', reason: 'Waiting on workers to finish the previous update' }] }],
];

let passed = 0;
const files = [];
async function run(viewport, touch, theme) {
	const tag = `${viewport.width}-${theme}`;
	const check = (condition, label, detail) => {
		assert.ok(condition, `[${tag}] ${label}${detail === undefined ? '' : `: ${JSON.stringify(detail)}`}`);
		passed++;
	};
	for (const [name, snapshot, how, expected, reply] of SCENARIOS.filter(([name]) => !ONLY || ONLY.includes(name))) {
		const context = await browser.newContext({ viewport, isMobile: touch, hasTouch: touch, deviceScaleFactor: 2, serviceWorkers: 'block',
			timezoneId: 'America/New_York', locale: 'en-US', colorScheme: theme });
		const posts = [];
		await context.route((u) => u.pathname.startsWith('/api/') || u.pathname === '/health', async (route) => {
			const request = route.request();
			const path = new URL(request.url()).pathname;
			if (path.endsWith('/settings/application-updates/workers')) return route.fulfill({ json: snapshot });
			if (request.method() === 'POST' && path.endsWith('/settings/application-updates')) {
				posts.push(request.postDataJSON());
				return route.fulfill({ json: reply ?? { results: [{ device_name: 'Current worker', accepted: true, reason: 'Request queued' }] } });
			}
			if (!shell) return route.abort();
			if (path.endsWith('/auth/status')) return route.fulfill({ json: AUTH });
			if (path.startsWith('/api/v1/assistant')) return; // never answered: the home page stays in its neutral loading state
			return route.fulfill({ status: 404, json: { detail: 'Not Found' } });
		});
		const page = await context.newPage();
		const errors = [];
		page.on('pageerror', (e) => errors.push(e.message));
		await page.goto(shell ? url : `${url}?theme=${theme}`);
		// On a phone the update button rides in the sidebar drawer.
		if (shell && touch) await page.getByRole('button', { name: 'Show sidebar' }).click();
		const button = page.locator('.update-button');
		await button.waitFor();
		const shot = async (suffix = '') => {
			if (!shots) return;
			const file = join(shots, `${name}${suffix}-${tag}.png`);
			await page.screenshot({ path: file });
			files.push(file);
		};
		const hover = async () => {
			if (touch) await button.focus(); else await button.hover();
			const tip = page.getByRole('tooltip');
			await tip.waitFor();
			await page.waitForTimeout(200);
			return tip;
		};
		if (how === 'celebrate') {
			await page.getByRole('button', { name: 'Update complete' }).waitFor({ timeout: 3000 });
			await page.waitForTimeout(450);
			await shot();
			check(await page.locator('.update-slot.complete').count() === 1, `${name}: complete pops`);
		} else if (how === 'check-first') {
			check(await button.getAttribute('aria-label') === 'Check for updates', `${name}: button checks`, await button.getAttribute('aria-label'));
			const tip = await hover();
			const text = (await tip.innerText()).replace(/\s+/g, ' ');
			check(text.includes(expected), `${name}: tooltip copy`, text);
			check(!/Update and restart/.test(text), `${name}: no install offer`, text);
			await shot('-tooltip');
			await button.click({ force: true });
			for (let i = 0; i < 40 && !posts.length; i++) await page.waitForTimeout(50);
			await page.waitForTimeout(200); // a second request would land here
			check(JSON.stringify(posts) === JSON.stringify([{ action: 'check' }]), `${name}: one check request`, posts);
		} else if (how === 'hover') {
			const tip = await hover();
			const text = await tip.innerText();
			check(text.replace(/\s+/g, ' ').includes(expected), `${name}: tooltip copy`, text);
			check(!/could not be confirmed/.test(text), `${name}: no startup-failure copy`, text);
			check((text.match(/the updater is idle/g) ?? []).length <= 1, `${name}: no repeated sentence`, text);
			const box = await tip.boundingBox();
			check(box && box.x >= 0 && box.x + box.width <= viewport.width, `${name}: tooltip in viewport`, box);
			await shot('-tooltip');
		} else {
			await button.click({ force: true });
			const panel = page.getByRole('dialog');
			await panel.waitFor();
			await page.waitForTimeout(500); // the dock fades cards in
			let text = (await panel.innerText()).replace(/\s+/g, ' ');
			if (how === 'not-sent') {
				await page.getByRole('button', { name: 'Restart when work finishes' }).click();
				await page.waitForFunction(() => document.querySelector('.update-button')?.getAttribute('title')?.includes('Not sent to'));
				check(JSON.stringify(posts) === JSON.stringify([{ action: 'install', mode: 'when_idle' }]), `${name}: one when-idle request`, posts);
				const tip = await hover();
				text = (await tip.innerText()).replace(/\s+/g, ' ');
				check(text.includes(expected), `${name}: receipt copy`, text);
				check(!/Current worker: Not sent/.test(text), `${name}: worker named once`, text);
				await shot('-tooltip');
			} else {
				check(text.includes(expected), `${name}: panel copy`, text);
				check(!/force/i.test(text), `${name}: no force option`, text);
				const box = await panel.boundingBox();
				check(box && box.x >= 0 && box.x + box.width <= viewport.width + 1, `${name}: panel in viewport`, box);
				if (name === 'mixed-check-first-blocked') {
					check(await page.getByRole('button', { name: 'Stop running work and restart' }).count() === 0, `${name}: no stop offer`);
					check(await page.getByRole('button', { name: 'Check for updates' }).isEnabled(), `${name}: check offered`);
				}
				if (name === 'stopping' || name === 'queued') {
					for (const label of ['Stop running work and restart', 'Restart when work finishes'])
						check(await page.getByRole('button', { name: label }).isDisabled(), `${name}: ${label} disabled`);
				}
				await shot('-panel');
			}
		}
		// The shell's own unrelated reads are answered 404; only fail on errors from this surface.
		const own = shell ? errors.filter((e) => !/Not Found/.test(e)) : errors;
		check(own.length === 0, `${name}: no page errors`, own);
		await context.close();
	}
}

try {
	for (const theme of ['dark', 'light']) {
		await run({ width: 1280, height: 800 }, false, theme);
		await run({ width: 390, height: 844 }, true, theme);
	}
	console.log(`update waiting: ${passed} checks passed`);
	if (files.length) console.log(files.join('\n'));
} finally {
	await browser.close();
}
