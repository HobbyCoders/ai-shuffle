// Real-browser check of the Vault's one-time approval card: it shows inline
// above the composer (no panel to open), names the resource and the account,
// states the effect, fits and approves at desktop and phone widths, and the
// composer's Vault chip opens the panel upward inside the viewport. The API is
// answered here.
//
// Run from frontend/ while the fixture is serving:
//   npx vite dev --config tests/vite.vault.config.js --host 127.0.0.1 --port 6917 --strictPort
//   node tests/verify-vault.mjs
// Needs browser-mcp's playwright-core (npm ci --ignore-scripts --prefix ../browser-mcp)
// and a Chrome/Chromium binary (CHROMIUM_PATH). VAULT_FIXTURE_URL overrides
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
const url = process.env.VAULT_FIXTURE_URL || 'http://127.0.0.1:6917/tests/vault.html?session=fixture-session';
const shots = process.env.SCREENSHOT_DIR;
const scenarios = [
	{ name: 'machine-command', grant: { kind: 'machine', label: 'NAS', target: 'admin@10.0.0.5:22', writable: true },
		action: { operation: 'command', arguments: { command: 'grep -rn "TODO" . | head -20' } }, shown: '$ grep -rn "TODO" . | head -20',
		title: 'Run this on NAS?', scope: 'Once, as admin@10.0.0.5:22', button: 'Run once',
		hint: 'It runs with that account’s authority and can change files on that machine. The next command asks again.' },
	{ name: 'service-change', grant: { kind: 'service', label: 'Inventory', target: 'https://api.example.com/v1', writable: true },
		action: { operation: 'http', arguments: { method: 'POST', path: '/items', body: '{"n":1}', content_type: 'application/json' } }, shown: 'POST /items\napplication/json\n\n{"n":1}',
		title: 'Send this to Inventory?', scope: 'Once, to https://api.example.com/v1', button: 'Send once',
		hint: 'It is sent with the sign-in attached; the agent never sees the token or password. The next change asks again.' },
	{ name: 'folder-write', grant: { kind: 'folder', label: 'Notes', target: '/home/me/notes', writable: true },
		action: { operation: 'write', arguments: { path: 'todo.md', content: '- call the plumber' } }, shown: 'todo.md\n\n- call the plumber',
		title: 'Write this file on Notes?', scope: 'Once, in /home/me/notes', button: 'Write once',
		hint: 'It writes this text once. The next write asks again.' },
	{ name: 'service-capture', grant: { kind: 'service', label: 'Cloudflare', target: 'https://api.cloudflare.com/client/v4', writable: true },
		action: { operation: 'http', arguments: { method: 'POST', path: '/accounts/acct/tokens', body: '{"name":"deploy"}', content_type: 'application/json', capture: 'result.value' } },
		shown: 'POST /accounts/acct/tokens\napplication/json\n\n{"name":"deploy"}',
		title: 'Send this to Cloudflare?', scope: 'Once, to https://api.cloudflare.com/client/v4', button: 'Send once',
		capture: 'The response’s result.value is kept hidden from the agent, so it can only be copied on with your approval.',
		hint: 'It is sent with the sign-in attached; the agent never sees the token or password. The next change asks again.' },
	{ name: 'copy-secret', grant: { kind: 'service', label: 'GitHub', target: 'https://api.github.com', writable: true },
		action: { operation: 'copy_secret', arguments: { secret: 'Cloudflare: POST /accounts/acct/tokens → result.value', secret_id: 'sec_1',
			repository: 'example-org/website', environment: 'production', name: 'CLOUDFLARE_API_TOKEN' } },
		shownIn: 'dl.pa-copy',
		shown: 'FromCloudflare: POST /accounts/acct/tokens → result.value Repositoryexample-org/website Environmentproduction SecretCLOUDFLARE_API_TOKEN',
		title: 'Copy a secret to GitHub?', scope: 'Once, with GitHub', button: 'Copy once',
		hint: 'The worker encrypts the value with GitHub’s key and writes it, replacing any secret of that name. The agent never sees it, and it is not saved in the Vault. Every copy asks, even in Bypass.' },
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
		let actions = [{ id: 'a1', grant_id: 'g1', expires_at: 9999999999, ...scenario.action }];
		await context.route((u) => u.pathname.startsWith('/api/'), async (route) => {
			const request = route.request();
			const path = new URL(request.url()).pathname;
			if (path.endsWith('/v1/sessions/fixture-session')) return route.fulfill({ json: { mode: 'chat' } });
			if (request.method() === 'POST' && path.endsWith('/vault/sessions/fixture-session/actions/a1')) {
				decisions.push(request.postDataJSON());
				actions = [];
				return route.fulfill({ json: {} });
			}
			if (path.endsWith('/vault/sessions/fixture-session'))
				return route.fulfill({ json: { grants: [grant], actions, requests: [], saved_connections: [], vault_available: false } });
			return route.fulfill({ json: {} });
		});
		const page = await context.newPage();
		const errors = [];
		page.on('pageerror', (e) => errors.push(e.message));
		await page.goto(url);
		const open = page.getByRole('button', { name: /^Vault/ }).first();
		await open.waitFor();
		await page.waitForFunction(() => document.querySelector('button[aria-label="Vault: 1 waiting for you"]'));
		// Inline above the composer: nothing to open before answering.
		const card = page.getByRole('group', { name: 'Vault: waiting for you' }).getByRole('region', { name: 'One-time approval' });
		await card.waitFor();
		check(await page.locator('.pa-pop:not([hidden])').count() === 0, 'the panel stays closed');
		check(await card.getByText(scenario.title).isVisible(), 'title', scenario.title);
		const hint = card.locator('.pa-hint').last();
		check((await hint.textContent()) === scenario.hint, 'hint', await hint.textContent());
		if (scenario.capture) {
			const kept = card.locator('.pa-hint').first();
			check((await kept.textContent()) === scenario.capture, 'captured field named', await kept.textContent());
		}
		const shown = card.locator(scenario.shownIn ?? 'pre.pa-mono');
		check((await shown.textContent()) === scenario.shown, 'action shown verbatim', await shown.textContent());
		check(await card.getByText(scenario.scope, { exact: true }).isVisible(), 'grant scope', scenario.scope);
		const fit = await page.evaluate(() => {
			const box = document.querySelector('.pa-lane').getBoundingClientRect();
			const composer = document.querySelector('.composer').getBoundingClientRect();
			return { left: box.left, right: box.right, top: box.top, bottom: box.bottom, composerTop: composer.top, composerBottom: composer.bottom,
				width: innerWidth, height: innerHeight, scroll: document.documentElement.scrollWidth,
				hint: document.querySelector('.pa-hint').scrollWidth <= document.querySelector('.pa-hint').clientWidth };
		});
		check(fit.left >= 0 && fit.right <= fit.width && fit.scroll <= fit.width && fit.hint, 'card and hint fit the viewport', fit);
		check(fit.top >= 0 && fit.bottom <= fit.composerTop + 1 && fit.composerBottom <= fit.height, 'card sits above a composer that stays on screen', fit);
		// The chip opens the panel inside the viewport: upward on desktop, a sheet on touch.
		if (touch) await open.tap(); else await open.click();
		const panel = page.getByRole('dialog', { name: 'Vault' });
		await panel.waitFor();
		await page.waitForTimeout(300);
		const pop = await panel.boundingBox();
		check(pop.x >= 0 && pop.y >= 0 && pop.x + pop.width <= viewport.width + 1 && pop.y + pop.height <= viewport.height + 1, 'panel fits the viewport', pop);
		check(await panel.getByText(grant.label, { exact: true }).isVisible(), 'panel lists the active access');
		check(await panel.getByRole('button', { name: /Manage saved sign-ins in Settings/ }).isVisible(), 'panel links to Settings');
		if (shots) await page.screenshot({ path: join(shots, `vault-${label.replace(/\s+/g, '-')}-panel.png`) });
		await panel.getByRole('button', { name: 'Close Vault' }).last().click();
		await panel.waitFor({ state: 'hidden' });
		const approve = card.getByRole('button', { name: scenario.button });
		const size = await approve.boundingBox();
		check(!touch || size.height >= 32, 'approve button is tappable', size);
		if (shots) await page.screenshot({ path: join(shots, `vault-${label.replace(/\s+/g, '-')}.png`) });
		if (touch) await approve.tap(); else await approve.click();
		await page.waitForFunction(() => !document.querySelector('[aria-label="One-time approval"]'));
		check(JSON.stringify(decisions) === JSON.stringify([{ allow: true }]), 'approve posts one decision', decisions);
		check(errors.length === 0, 'no page errors', errors);
		await context.close();
	}
}
await browser.close();
console.log(`vault approval card: ${passed} checks passed`);
