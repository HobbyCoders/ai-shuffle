// Real-browser check of the private-access approval card (#1267): a command on a
// folder grant reads as a sandboxed command with the folder's write effect, a
// command on a machine grant still reads as remote, and the card fits and
// approves at desktop and phone widths. The API is answered here.
//
// Run from frontend/ while the fixture is serving:
//   npx vite dev --config tests/vite.chat-access.config.js --host 127.0.0.1 --port 6917 --strictPort
//   node tests/verify-chat-access.mjs
// Needs browser-mcp's playwright-core (npm ci --ignore-scripts --prefix ../browser-mcp)
// and a Chrome/Chromium binary (CHROMIUM_PATH). CHAT_ACCESS_FIXTURE_URL overrides
// the URL; SCREENSHOT_DIR saves a screenshot per scenario.
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
const url = process.env.CHAT_ACCESS_FIXTURE_URL || 'http://127.0.0.1:6917/tests/chat-access.html?session=fixture-session';
const shots = process.env.SCREENSHOT_DIR;
const SANDBOX = 'Runs once in an isolated sandbox that sees only this folder and has no network.';
const scenarios = [
	{ name: 'folder-writable', grant: { kind: 'folder', label: 'Notes', target: '/home/me/notes', writable: true },
		title: 'Approve this sandboxed command?', hint: `${SANDBOX} It can change files in the folder.` },
	{ name: 'folder-read-only', grant: { kind: 'folder', label: 'Notes', target: '/home/me/notes', writable: false },
		title: 'Approve this sandboxed command?', hint: `${SANDBOX} The folder is read-only to it.` },
	{ name: 'machine', grant: { kind: 'machine', label: 'NAS', target: 'admin@10.0.0.5:22', writable: true },
		title: 'Approve this remote command?', hint: 'Runs once on the server with this account’s authority. It can change files and use the server’s network.' },
];
let passed = 0;

for (const [viewport, touch] of [[{ width: 1280, height: 800 }, false], [{ width: 390, height: 844 }, true]]) {
	for (const scenario of scenarios) {
		const label = `${viewport.width}x${viewport.height} ${scenario.name}`;
		const check = (condition, what, detail) => {
			assert.ok(condition, `[${label}] ${what}${detail === undefined ? '' : `: ${JSON.stringify(detail)}`}`);
			passed++;
		};
		const context = await browser.newContext({ viewport, isMobile: touch, hasTouch: touch, deviceScaleFactor: 1, serviceWorkers: 'block' });
		const decisions = [];
		const grant = { id: 'g1', expires_at: 9999999999, fingerprint: '', ...scenario.grant };
		let actions = [{ id: 'a1', grant_id: 'g1', operation: 'command', arguments: { command: 'grep -rn "TODO" . | head -20' }, expires_at: 9999999999 }];
		await context.route((u) => u.pathname.startsWith('/api/'), async (route) => {
			const request = route.request();
			const path = new URL(request.url()).pathname;
			if (path.endsWith('/sessions/fixture-session')) return route.fulfill({ json: { mode: 'chat' } });
			if (request.method() === 'POST' && path.endsWith('/chat-access/fixture-session/actions/a1')) {
				decisions.push(request.postDataJSON());
				actions = [];
				return route.fulfill({ json: {} });
			}
			if (path.endsWith('/chat-access/fixture-session'))
				return route.fulfill({ json: { grants: [grant], actions, requests: [], saved_connections: [], vault_available: false } });
			return route.fulfill({ json: {} });
		});
		const page = await context.newPage();
		const errors = [];
		page.on('pageerror', (e) => errors.push(e.message));
		await page.goto(url);
		const open = page.getByRole('button', { name: /^Private access/ }).first();
		await open.waitFor();
		await page.waitForFunction(() => document.querySelector('button[aria-label="Private access: 1 waiting for you"]'));
		if (touch) await open.tap(); else await open.click();
		const card = page.getByRole('region', { name: 'One-shot action approval' });
		await card.waitFor();
		check(await card.getByText(scenario.title).isVisible(), 'title', scenario.title);
		check((await card.locator('.pa-hint').textContent()) === scenario.hint, 'hint', await card.locator('.pa-hint').textContent());
		check((await card.locator('pre.pa-mono').textContent()) === '$ grep -rn "TODO" . | head -20', 'command shown verbatim');
		check(await card.getByText(`${grant.label} · ${grant.target}`).isVisible(), 'grant scope');
		const fit = await page.evaluate(() => {
			const dialog = document.querySelector('.pa-pop:not([hidden])');
			const box = dialog.getBoundingClientRect();
			return { left: box.left, right: box.right, width: innerWidth, scroll: document.documentElement.scrollWidth,
				hint: document.querySelector('.pa-hint').scrollWidth <= document.querySelector('.pa-hint').clientWidth };
		});
		check(fit.left >= 0 && fit.right <= fit.width && fit.scroll <= fit.width && fit.hint, 'panel and hint fit the viewport', fit);
		const approve = card.getByRole('button', { name: 'Approve once' });
		const size = await approve.boundingBox();
		check(!touch || size.height >= 32, 'approve button is tappable', size);
		if (shots) await page.screenshot({ path: join(shots, `chat-access-${label.replace(/\s+/g, '-')}.png`) });
		if (touch) await approve.tap(); else await approve.click();
		await page.waitForFunction(() => !document.querySelector('[aria-label="One-shot action approval"]'));
		check(JSON.stringify(decisions) === JSON.stringify([{ allow: true }]), 'approve posts one decision', decisions);
		check(errors.length === 0, 'no page errors', errors);
		await context.close();
	}
}
await browser.close();
console.log(`chat-access approval card: ${passed} checks passed`);
