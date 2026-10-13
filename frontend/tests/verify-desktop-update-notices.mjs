// Real-browser check that desktop updates are handled in the web UI alone, with
// no native prompts: a macOS/Linux installer release is offered and downloaded
// from the sidebar button and the bell says where the installer went; a staged
// update that did not install (sessions running, or a failed install) shows in
// the button and the bell, and its retry goes through the shared control.
//
// Run from frontend/ while the fixture is serving:
//   npx vite dev --config tests/vite.update-button.config.js --host 127.0.0.1 --port 5247 --strictPort
//   node tests/verify-desktop-update-notices.mjs
// Needs browser-mcp's playwright-core (npm ci --ignore-scripts --prefix ../browser-mcp)
// and a Chrome/Chromium binary (CHROMIUM_PATH). DESKTOP_NOTICES_FIXTURE_URL overrides
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
const url = process.env.DESKTOP_NOTICES_FIXTURE_URL || 'http://127.0.0.1:5247/tests/desktop-update-notices.html';
const shots = process.env.SCREENSHOT_DIR;
const viewport = { width: 1280, height: 800 };
let passed = 0;
const check = (condition, label, detail) => {
	assert.ok(condition, `${label}${detail === undefined ? '' : `: ${JSON.stringify(detail)}`}`);
	passed++;
};
const shot = async (page, file) => { if (shots) await page.screenshot({ path: join(shots, `${file}.png`) }); };

const mac = { device_id: 'local', device_name: 'This Mac', is_self: true, kind: 'desktop', online: true, supported: true, allow_updates: true, checked_at: 1_999_000 };
const scope = (worker) => ({ allow_updates: true, update_all_workers: false, server_time: 2_000_000, workers: [worker] });

async function open(snapshot) {
	const context = await browser.newContext({ viewport, deviceScaleFactor: 1, serviceWorkers: 'block' });
	const state = { snapshot, posts: [] };
	await context.route((u) => u.pathname.startsWith('/api/'), async (route) => {
		const request = route.request();
		if (request.url().endsWith('/settings/application-updates/workers')) return route.fulfill({ json: state.snapshot });
		if (request.method() === 'POST' && request.url().endsWith('/settings/application-updates')) {
			state.posts.push(request.postDataJSON());
			return route.fulfill({ json: { results: [{ device_id: 'local', device_name: 'This Mac', accepted: true, reason: 'Request queued' }] } });
		}
		return route.abort();
	});
	const page = await context.newPage();
	const errors = [];
	page.on('pageerror', (e) => errors.push(e.message));
	await page.goto(url);
	return { context, page, state, errors };
}
const button = (page) => page.locator('.update-button');
async function bell(page) {
	await page.locator('.notif-btn').click();
	const panel = page.getByRole('dialog', { name: 'Notifications' });
	await panel.waitFor();
	// Let the panel's open transition finish before reading or capturing it.
	await page.waitForTimeout(400);
	return panel;
}

// 1. macOS installer release: offered by the sidebar button, downloaded on click,
//    and the bell names the saved file.
let run = await open(scope({ ...mac, phase: 'available', version: '0.9.0', running_version: null, release: '0.9.0',
	message: 'AI Shuffle 0.9.0 is available. Download the installer to update.' }));
await run.page.getByRole('button', { name: 'Download update' }).waitFor();
await button(run.page).hover();
let tip = run.page.getByRole('tooltip');
await tip.waitFor();
let text = await tip.innerText();
check(/This Mac\s+Update available; not downloaded/.test(text), 'the installer release is offered in the web UI', text);
await shot(run.page, '1-installer-offered');
await button(run.page).click();
await run.page.waitForTimeout(200);
check(JSON.stringify(run.state.posts) === JSON.stringify([{ action: 'download' }]), 'the button asks the worker to download', run.state.posts);
const file = '/Users/me/Downloads/AI-Shuffle-0.9.0.dmg';
run.state.snapshot = scope({ ...mac, phase: 'idle', version: null, running_version: null,
	message: `AI Shuffle 0.9.0 was downloaded and verified. Open ${file} to install it.` });
await run.page.evaluate((saved) => window.__emitDesktopUpdate({ phase: 'idle', version: null, percent: null, error: null, checkedAt: null, installer: saved,
	message: `AI Shuffle 0.9.0 was downloaded and verified. Open ${saved} to install it.` }), file);
await run.page.mouse.move(0, viewport.height - 1);
let panel = await bell(run.page);
text = await panel.innerText();
check(text.includes('Installer downloaded'), 'the bell announces the downloaded installer', text);
check(text.includes(file), 'the bell says where the installer was saved', text);
await shot(run.page, '2-installer-saved');
check(run.errors.length === 0, 'no page errors (installer)', run.errors);
await run.context.close();

// 2. Staged update refused while sessions run: the button shows why, the bell
//    offers Try again, and the retry is the shared control's install request.
const busy = 'Busy with active sessions; retry when finished.';
run = await open(scope({ ...mac, phase: 'ready', version: 'cccccccccccc', running_version: 'b'.repeat(40), error: busy }));
await run.page.getByRole('button', { name: 'Update and restart' }).waitFor();
check(await button(run.page).evaluate((el) => el.classList.contains('failed')), 'the button shows the update needs attention');
await button(run.page).hover();
tip = run.page.getByRole('tooltip');
await tip.waitFor();
text = await tip.innerText();
check(text.includes('Update needs attention') && text.includes(busy), 'the hover card gives the reason', text);
await shot(run.page, '3-busy-button');
await run.page.evaluate((error) => window.__emitDesktopUpdate({ phase: 'ready', version: 'cccccccccccc', percent: null, error, checkedAt: null }), busy);
await run.page.mouse.move(0, viewport.height - 1);
panel = await bell(run.page);
text = await panel.innerText();
check(text.includes('Update not installed') && text.includes(busy), 'the bell reports the refused install', text);
const box = await panel.boundingBox();
check(box && box.x >= 0 && box.y >= 0 && box.x + box.width <= viewport.width && box.y + box.height <= viewport.height, 'bell panel within viewport', box);
await shot(run.page, '4-busy-bell');
// Sessions finished: Try again goes through the shared control, never the shell's local install.
await panel.getByRole('button', { name: 'Try again' }).click();
await run.page.waitForTimeout(300);
check(JSON.stringify(run.state.posts) === JSON.stringify([{ action: 'install' }]), 'Try again requests the install through the worker', run.state.posts);
check(await run.page.evaluate(() => window.__nativeInstalls) === 0, 'the shell install is never called directly');
check(run.errors.length === 0, 'no page errors (busy)', run.errors);
await run.context.close();

// 3. A failed install after the worker came back: reported in the bell, not a native box.
run = await open(scope({ ...mac, phase: 'ready', version: 'cccccccccccc', running_version: 'b'.repeat(40), error: 'Error: Could not stage the update' }));
await run.page.getByRole('button', { name: 'Update and restart' }).waitFor();
await run.page.evaluate(() => window.__emitDesktopUpdate({ phase: 'ready', version: 'cccccccccccc', percent: null, error: 'Error: Could not stage the update', checkedAt: null }));
panel = await bell(run.page);
text = await panel.innerText();
check(text.includes('Update not installed') && text.includes('Could not stage the update'), 'the bell reports the failed install', text);
await shot(run.page, '5-failed-bell');
check(run.errors.length === 0, 'no page errors (failed)', run.errors);
await run.context.close();

await browser.close();
console.log(`desktop update notices: ${passed} checks passed`);
