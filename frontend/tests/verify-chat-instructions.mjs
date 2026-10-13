// Real-browser check of Settings > Security > Chat instructions: the row uses
// the new name, a saved customization says it differs from the current
// default, the default opens read-only for comparison without saving anything,
// and Revert to default (after its confirm) restores the default. Desktop width;
// no horizontal overflow.
//
// Reuses the standalone Settings fixture. Run from frontend/ while it serves:
//   npx vite dev --config tests/vite.settings-plugins.config.js --host 127.0.0.1 --port 6401 --strictPort
//   node tests/verify-chat-instructions.mjs
// Needs browser-mcp's playwright-core (npm ci --ignore-scripts --prefix ../browser-mcp)
// and a Chrome/Chromium binary (CHROMIUM_PATH). CHAT_INSTRUCTIONS_FIXTURE_URL
// overrides the URL; SCREENSHOT_DIR saves a screenshot per step.
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
const base = process.env.CHAT_INSTRUCTIONS_FIXTURE_URL || 'http://127.0.0.1:6401/tests/settings-plugins.html';
const shots = process.env.SCREENSHOT_DIR;

const DEFAULT_TEXT = '## Chat display\nThe chat renders GitHub-flavored Markdown.\n\n### Files on disk\nPercent-encode the whole absolute path.';
const CUSTOM_TEXT = '## Showing Files & Media in Chat\nMy hand-edited rules.';

let passed = 0;
async function harness(viewport) {
	const name = `${viewport.width}x${viewport.height}`;
	const check = (condition, label, detail) => {
		assert.ok(condition, `[${name}] ${label}${detail === undefined ? '' : `: ${JSON.stringify(detail)}`}`);
		passed++;
	};
	const shot = async (page, file) => { if (shots) await page.screenshot({ path: join(shots, `chat-instructions-${name}-${file}.png`), fullPage: true }); };
	const context = await browser.newContext({ viewport, deviceScaleFactor: 1, serviceWorkers: 'block' });
	const state = { saved: CUSTOM_TEXT, writes: [] };
	await context.route((u) => u.pathname.startsWith('/api/'), async (route) => {
		const request = route.request();
		const path = new URL(request.url()).pathname.replace('/api/v1', '');
		if (path === '/auth/status') return route.fulfill({ json: { authenticated: true, setup_required: false, is_admin: true, username: 'owner' } });
		if (path === '/settings/security-instructions') {
			if (request.method() === 'GET') {
				return route.fulfill({ json: { current: state.saved ?? DEFAULT_TEXT, default: DEFAULT_TEXT, is_customized: state.saved !== null } });
			}
			state.writes.push(request.method());
			if (request.method() === 'DELETE') state.saved = null;
			return route.fulfill({ json: { success: true } });
		}
		return route.fulfill({ status: 404, json: { detail: 'not in this fixture' } });
	});
	const page = await context.newPage();
	const errors = [];
	page.on('pageerror', (e) => errors.push(e.message));
	const noOverflow = () => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);

	// 1. Security lists the setting under its new name, customized.
	await page.goto(`${base}?tab=security`);
	const row = page.getByRole('button', { name: /^Chat instructions/ });
	await row.waitFor();
	check((await row.textContent()).includes('Customized'), 'row says Customized', await row.textContent());
	check((await page.getByText('Security instructions', { exact: true }).count()) === 0, 'old name gone from the page');

	// 2. The editor says the saved text differs and offers the default.
	await row.click();
	const editor = page.getByRole('textbox', { name: 'Chat instructions', exact: true });
	await editor.waitFor();
	check((await editor.inputValue()) === CUSTOM_TEXT, 'editor shows the saved text');
	await page.getByText('Your saved text differs from the current default.').waitFor();
	const toggle = page.getByRole('button', { name: 'View default' });
	check((await toggle.getAttribute('aria-expanded')) === 'false', 'default is collapsed at first');
	await shot(page, '1-differs');

	// 3. The default opens read-only next to the editor; nothing is written.
	await toggle.click();
	const shown = page.getByLabel('Current default chat instructions');
	await shown.waitFor();
	check((await shown.inputValue()) === DEFAULT_TEXT, 'read-only view shows the current default');
	check(await shown.evaluate((el) => el.readOnly), 'default view is read-only');
	check((await editor.inputValue()) === CUSTOM_TEXT, 'own text untouched while comparing');
	check((await noOverflow()) <= 0, 'no horizontal overflow with both texts open', await noOverflow());
	check(state.writes.length === 0, 'viewing the default writes nothing', state.writes);
	await shot(page, '2-compare');
	await page.getByRole('button', { name: 'Hide default' }).click();
	check((await shown.count()) === 0, 'Hide default closes the view');

	// 4. Revert asks first, then restores the default and drops the note.
	await page.getByRole('button', { name: 'Revert to default' }).click();
	await page.getByRole('button', { name: 'Revert', exact: true }).click();
	await page.getByText('Your saved text differs from the current default.').waitFor({ state: 'detached' });
	check(JSON.stringify(state.writes) === JSON.stringify(['DELETE']), 'one DELETE after confirming', state.writes);
	check((await editor.inputValue()) === DEFAULT_TEXT, 'editor holds the default after revert');
	check((await row.textContent()).includes('Default'), 'row says Default after revert', await row.textContent());
	await shot(page, '3-reverted');
	check(errors.length === 0, 'no page errors', errors);
	await context.close();
}

try {
	await harness({ width: 1280, height: 800 });
	console.log(`chat-instructions: ${passed} checks passed`);
} finally {
	await browser.close();
}
