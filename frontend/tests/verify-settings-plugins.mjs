// Real-browser check that plugin management lives in Settings (#1610): a
// "Manage plugins" deep link opens Settings on its Plugins section, the plugin
// hub lists an agent-authored plugin as needing review, its details show every
// component (with the MCP server's command line), and approving sends the
// reviewed content hash. Desktop and phone widths; no horizontal overflow.
//
// Run from frontend/ while the fixture is serving:
//   npx vite dev --config tests/vite.settings-plugins.config.js --host 127.0.0.1 --port 6401 --strictPort
//   node tests/verify-settings-plugins.mjs
// Needs browser-mcp's playwright-core (npm ci --ignore-scripts --prefix ../browser-mcp)
// and a Chrome/Chromium binary (CHROMIUM_PATH). SETTINGS_PLUGINS_FIXTURE_URL
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
const base = process.env.SETTINGS_PLUGINS_FIXTURE_URL || 'http://127.0.0.1:6401/tests/settings-plugins.html';
const shots = process.env.SCREENSHOT_DIR;

const plugin = (overrides) => ({
	description: '', version: '1.0.0', scope: 'user', install_path: '/home/user/.fluid/plugins/cache/x',
	installed_at: '2026-10-01T00:00:00', enabled: true, has_commands: false, has_agents: false,
	has_skills: true, has_hooks: false, has_mcp: false, connector_type: null, format: 'claude',
	trust: 'marketplace', components: [], approval: null, content_hash: null, ...overrides,
});
const reviewkit = plugin({ id: 'reviewkit@mp', name: 'reviewkit', marketplace: 'mp', description: 'Review helpers' });
const authoredComponents = [
	{ kind: 'skill', name: 'draft-notes', support: 'loaded', reason: '', detail: '' },
	{ kind: 'mcp_server', name: 'changelog', support: 'loaded', reason: '', detail: 'python ${PLUGIN_ROOT}/srv.py' },
];
const authored = plugin({
	id: 'release-notes@authored', name: 'release-notes', marketplace: 'authored', version: '0.1.0',
	description: 'Drafts release notes', enabled: false, has_mcp: true, format: 'agent-plugins',
	trust: 'authored', approval: 'pending', content_hash: 'sha256:reviewed', components: authoredComponents,
});
const details = (p) => ({ ...p, commands: [], agents: [], skills: ['draft-notes'], readme: null });

let passed = 0;
async function harness(viewport, touch) {
	const name = `${viewport.width}x${viewport.height}`;
	const check = (condition, label, detail) => {
		assert.ok(condition, `[${name}] ${label}${detail === undefined ? '' : `: ${JSON.stringify(detail)}`}`);
		passed++;
	};
	const shot = async (page, file) => { if (shots) await page.screenshot({ path: join(shots, `${name}-${file}.png`), fullPage: true }); };
	const context = await browser.newContext({ viewport, isMobile: touch, hasTouch: touch, deviceScaleFactor: 1, serviceWorkers: 'block' });
	const state = { enables: [], approved: false };
	await context.route((u) => u.pathname.startsWith('/api/'), async (route) => {
		const request = route.request();
		const path = new URL(request.url()).pathname.replace('/api/v1', '');
		const current = { ...authored, ...(state.approved ? { approval: 'approved', enabled: true } : {}) };
		if (path === '/auth/status') return route.fulfill({ json: { authenticated: true, setup_required: false, is_admin: true, username: 'owner' } });
		if (path === '/plugins/installed') return route.fulfill({ json: [reviewkit, current] });
		if (path === '/plugins/available') return route.fulfill({ json: [] });
		if (path === `/plugins/${encodeURIComponent(authored.id)}` || path === `/plugins/${authored.id}`) return route.fulfill({ json: details(current) });
		if (request.method() === 'POST' && path.endsWith('/enable')) {
			state.enables.push({ path, body: request.postDataJSON() });
			state.approved = true;
			return route.fulfill({ json: { status: 'enabled', plugin_id: authored.id } });
		}
		if (path.startsWith('/connectors')) return route.fulfill({ json: [] });
		return route.fulfill({ status: 404, json: { detail: 'not in this fixture' } });
	});
	const page = await context.newPage();
	const errors = [];
	page.on('pageerror', (e) => errors.push(e.message));

	const index = page.getByRole('navigation', { name: 'Settings' });
	const noOverflow = () => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
	const item = (id) => page.locator(`[data-settings-item="${id}"]`);

	// 1. The deep link lands on Settings > Plugins, with the draft under Needs review.
	await page.goto(`${base}?tab=plugins`);
	const draftRow = item('plugin:release-notes@authored');
	await draftRow.waitFor();
	if (await index.isVisible()) {
		const current = await index.getByRole('button', { name: /^Plugins/ }).getAttribute('aria-current');
		check(current === 'page', 'Plugins is the current Settings category', current);
	}
	// Which group label precedes each row in the list.
	const groupOf = (id) => page.evaluate((id) => {
		let el = document.querySelector(`[data-settings-item="${id}"]`);
		while (el && !el.classList?.contains('st-lg')) el = el.previousElementSibling;
		return el?.textContent?.trim() ?? null;
	}, id);
	check((await groupOf('plugin:release-notes@authored')) === 'Needs review', 'authored plugin listed under Needs review', await groupOf('plugin:release-notes@authored'));
	check((await groupOf('plugin:reviewkit@mp')) !== 'Needs review', 'marketplace plugin not under Needs review', await groupOf('plugin:reviewkit@mp'));
	check((await noOverflow()) <= 0, 'no horizontal overflow on the list', await noOverflow());
	await shot(page, '1-list');

	// 2. The draft opens on its review: version, every component, the MCP command line.
	await draftRow.click();
	const review = page.getByTestId('plugin-review');
	await review.waitFor();
	const text = await review.innerText();
	check(text.includes('0.1.0'), 'review names the version', text);
	check(/Skill\s+draft-notes/i.test(text), 'review lists the skill', text);
	check(text.includes('python ${PLUGIN_ROOT}/srv.py'), 'review shows the MCP command line', text);
	check((await noOverflow()) <= 0, 'no horizontal overflow on the review', await noOverflow());
	await shot(page, '2-review');

	// 3. Approving (with its inline confirm) sends exactly the reviewed hash, then the review is gone.
	await review.getByRole('button', { name: 'Approve and enable' }).click();
	await review.getByRole('button', { name: 'Approve', exact: true }).click();
	await review.waitFor({ state: 'detached' });
	check(state.enables.length === 1, 'one enable request', state.enables);
	check(JSON.stringify(state.enables[0].body) === JSON.stringify({ content_hash: 'sha256:reviewed' }), 'enable carries the reviewed hash', state.enables[0]);
	check(state.enables[0].path.includes('release-notes@authored') || state.enables[0].path.includes('release-notes%40authored'), 'enable targets the authored plugin', state.enables[0].path);
	await shot(page, '3-approved');

	// 4. Without a deep link (and a fresh session: Settings reopens where it was
	// left within a session) Plugins is not current, and it is one click away.
	await page.evaluate(() => sessionStorage.clear());
	await page.goto(base);
	await index.waitFor();
	const plugins = index.getByRole('button', { name: /^Plugins/ });
	check((await plugins.getAttribute('aria-current')) !== 'page', 'Plugins not current without a deep link');
	await plugins.click();
	await item('plugin:reviewkit@mp').waitFor();
	check(true, 'Plugins opens from the Settings index');
	check(errors.length === 0, 'no page errors', errors);
	await context.close();
}

try {
	await harness({ width: 1280, height: 800 }, false);
	await harness({ width: 390, height: 844 }, true);
	console.log(`settings-plugins: ${passed} checks passed`);
} finally {
	await browser.close();
}
