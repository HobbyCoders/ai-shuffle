// Real-browser check of the shared update button (#1623): all-worker readiness,
// per-worker phase rows in the hover card, and no completion pop on a reload
// while an update is staged. The update API is answered here, per scenario.
//
// Run from frontend/ while the fixture is serving:
//   npx vite dev --config tests/vite.update-button.config.js --host 127.0.0.1 --port 5247 --strictPort
//   node tests/verify-update-button.mjs
// Needs browser-mcp's playwright-core (npm ci --ignore-scripts --prefix ../browser-mcp)
// and a Chrome/Chromium binary (CHROMIUM_PATH). UPDATE_BUTTON_FIXTURE_URL overrides
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
const url = process.env.UPDATE_BUTTON_FIXTURE_URL || 'http://127.0.0.1:5247/tests/update-button.html';
const shots = process.env.SCREENSHOT_DIR;

const self = { device_id: 'local', device_name: 'Current worker', online: true, supported: true, allow_updates: true, version: 'cccccccccccc', running_version: 'b'.repeat(40) };
const unraid = { ...self, device_id: 'unraid', device_name: 'ServerWorker' };
const all = (workers, extra = {}) => ({ allow_updates: true, update_all_workers: true, server_time: 2_000_000, workers, ...extra });
const finished = (completedAt) => ({ id: 'install-1', phase: 'complete', completed_at: completedAt, workers: [
	{ device_id: 'local', device_name: 'Current worker', status: 'verified', message: 'Updated and verified' },
	{ device_id: 'unraid', device_name: 'ServerWorker', status: 'verified', message: 'Updated and verified' },
] });
let passed = 0;

async function harness(viewport, touch) {
	const name = `${viewport.width}x${viewport.height}`;
	const check = (condition, label, detail) => {
		assert.ok(condition, `[${name}] ${label}${detail === undefined ? '' : `: ${JSON.stringify(detail)}`}`);
		passed++;
	};
	// A fresh context per scenario is a fresh browser profile: empty localStorage.
	async function open(snapshot) {
		const context = await browser.newContext({ viewport, isMobile: touch, hasTouch: touch, deviceScaleFactor: 1, serviceWorkers: 'block' });
		const state = { snapshot, posts: [] };
		await context.route((u) => u.pathname.startsWith('/api/'), async (route) => {
			const request = route.request();
			if (request.url().endsWith('/settings/application-updates/workers'))
				return route.fulfill({ json: state.snapshot });
			if (request.method() === 'POST' && request.url().endsWith('/settings/application-updates')) {
				state.posts.push(request.postDataJSON());
				return route.fulfill({ json: { results: [{ device_name: 'Current worker', accepted: true, reason: 'Request queued' }] } });
			}
			return route.abort();
		});
		// Record whether the completion pop ever starts, however briefly.
		await context.addInitScript(() => {
			window.__popped = 0;
			new MutationObserver((records) => {
				for (const record of records)
					if (record.target instanceof Element && record.target.classList.contains('complete')) window.__popped++;
			}).observe(document, { subtree: true, attributes: true, attributeFilter: ['class'], childList: true });
			new MutationObserver(() => {
				if (document.querySelector('.update-slot.complete')) window.__popped++;
			}).observe(document, { subtree: true, childList: true });
		});
		const page = await context.newPage();
		const errors = [];
		page.on('pageerror', (e) => errors.push(e.message));
		await page.goto(url);
		return { context, page, state, errors };
	}
	const button = (page) => page.locator('.update-button');
	async function hover(page, target) {
		// Touch has no hover: the card opens on focus, as it does for a keyboard.
		if (touch) { await target.evaluate((el) => el.blur()); await target.focus(); } else await target.hover();
		const tip = page.getByRole('tooltip');
		await tip.waitFor();
		return tip;
	}
	async function inViewport(page, locator, label) {
		const box = await locator.boundingBox();
		check(box && box.x >= 0 && box.y >= 0 && box.x + box.width <= viewport.width && box.y + box.height <= viewport.height, `${label} within viewport`, box);
	}
	const shot = async (page, file) => { if (shots) await page.screenshot({ path: join(shots, `${name}-${file}.png`) }); };
	const refresh = (page) => page.evaluate(() => window.dispatchEvent(new Event('application-update-policy')));

	// 1. Desktop staged, ServerWorker still building: no restart, real phase per worker.
	let run = await open(all([{ ...self, phase: 'ready' }, { ...unraid, phase: 'downloading', message: 'Building frontend…' }]));
	await button(run.page).waitFor();
	check(await button(run.page).getAttribute('aria-label') === 'Downloading update', 'mixed phases are not ready', await button(run.page).getAttribute('aria-label'));
	check(await button(run.page).getAttribute('aria-disabled') === 'true', 'mixed phases cannot be clicked');
	let tip = await hover(run.page, button(run.page));
	let text = await tip.innerText();
	check(text.includes('1 of 2 staged'), 'summary counts staged workers', text);
	check(/Current worker\s+Staged, awaiting restart/.test(text), 'staged worker row', text);
	check(/ServerWorker\s+Building frontend…/.test(text), 'building worker row', text);
	await inViewport(run.page, tip, 'mixed tooltip');
	await shot(run.page, '1-mixed');
	await button(run.page).click({ force: true });
	check(run.state.posts.length === 0, 'no request while preparing', run.state.posts);

	// 2. Both staged: one restart action, and the hover says staged, not running.
	run.state.snapshot = all([{ ...self, phase: 'ready' }, { ...unraid, phase: 'ready', message: 'Update ready. Choose Update and restart.' }]);
	await refresh(run.page);
	await run.page.getByRole('button', { name: 'Update and restart' }).waitFor();
	await run.page.mouse.move(0, viewport.height - 1);
	tip = await hover(run.page, button(run.page));
	text = await tip.innerText();
	check(/ServerWorker\s+Staged, awaiting restart/.test(text), 'staged ServerWorker row', text);
	check(!/running/i.test(text), 'no running text once staged', text);
	await inViewport(run.page, tip, 'ready tooltip');
	await shot(run.page, '2-ready');
	await button(run.page).click();
	await run.page.waitForTimeout(150);
	check(JSON.stringify(run.state.posts) === JSON.stringify([{ action: 'install' }]), 'second click installs once', run.state.posts);
	check(run.errors.length === 0, 'no page errors', run.errors);
	await run.context.close();

	// 2b. An unreadable peer holds the restart and says why.
	run = await open(all([{ ...self, phase: 'ready' }, { ...unraid, phase: 'unknown', supported: false }]));
	await run.page.getByRole('button', { name: 'Check for updates' }).waitFor();
	tip = await hover(run.page, button(run.page));
	text = await tip.innerText();
	check(text.includes('Restart waits for ServerWorker (Status unavailable)'), 'held summary names the unknown worker', text);
	check(/ServerWorker\s+Status unavailable/.test(text), 'unknown worker row', text);
	await inViewport(run.page, tip, 'held tooltip');
	await shot(run.page, '2b-held');
	await run.context.close();

	// 3. Fresh profile, staged update, an older completed install on record: no pop.
	for (const [label, completedAt] of [['old completion', 1_000_000], ['recent completion', 1_999_990]]) {
		run = await open(all([{ ...self, phase: 'ready' }, { ...unraid, phase: 'ready' }], { operation: finished(completedAt) }));
		await run.page.getByRole('button', { name: 'Update and restart' }).waitFor();
		await run.page.waitForTimeout(2500);
		check(await run.page.evaluate(() => window.__popped) === 0, `no pop on reload while staged (${label})`);
		check(await run.page.getByRole('button', { name: 'Update and restart' }).isVisible(), `restart stays offered (${label})`);
		await shot(run.page, `3-reload-staged-${label.replace(' ', '-')}`);
		await run.page.reload();
		await run.page.getByRole('button', { name: 'Update and restart' }).waitFor();
		await run.page.waitForTimeout(500);
		check(await run.page.evaluate(() => window.__popped) === 0, `no pop on second reload (${label})`);
		await run.context.close();
	}

	// 4. Fresh profile, nothing staged, old completion: stays hidden.
	run = await open(all([{ ...self, phase: 'idle', version: null }], { operation: finished(1_000_000) }));
	await run.page.waitForTimeout(1000);
	check(await run.page.evaluate(() => window.__popped) === 0, 'no pop for history');
	check(await button(run.page).count() === 0, 'button hidden for history');
	await run.context.close();

	// 5. Positive control: the app restarted into a just-finished install.
	run = await open(all([{ ...self, phase: 'idle', version: null }], { operation: finished(1_999_990) }));
	await run.page.waitForFunction(() => window.__popped > 0, null, { timeout: 5000 });
	check(true, 'pops for a just-finished install');
	await run.page.waitForTimeout(2500);
	check(await button(run.page).count() === 0, 'button leaves after the pop');
	await run.page.reload();
	await run.page.waitForTimeout(1000);
	check(await run.page.evaluate(() => window.__popped) === 0, 'no second pop after reload');
	await run.context.close();
}

try {
	await harness({ width: 1440, height: 900 }, false);
	await harness({ width: 390, height: 844 }, true);
	console.log(`update button: ${passed} checks passed`);
} finally {
	await browser.close();
}
