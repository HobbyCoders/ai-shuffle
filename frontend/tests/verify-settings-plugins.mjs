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
	check((await groupOf('plugin:release-notes@authored')) === 'Needs your review', 'authored plugin listed under Needs your review', await groupOf('plugin:release-notes@authored'));
	check((await groupOf('plugin:reviewkit@mp')) !== 'Needs your review', 'marketplace plugin not under Needs your review', await groupOf('plugin:reviewkit@mp'));
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

// The plugin review of 2026-10-03: what the owner sees in each state.
// `world` is the fake backend for one scenario; each scenario reloads the page.
const hooked = plugin({
	id: 'guard@mp', name: 'guard', marketplace: 'mp', description: 'Blocks risky shell commands', enabled: false,
	has_hooks: true, has_mcp: true,
	components: [
		{ kind: 'skill', name: 'triage', support: 'loaded', reason: '', detail: '' },
		{ kind: 'hook', name: 'PreToolUse', support: 'loaded', reason: '', detail: 'Bash: ${CLAUDE_PLUGIN_ROOT}/scripts/guard.sh --strict --log ~/.guard/log.txt' },
		{ kind: 'hook', name: 'SessionStart', support: 'loaded', reason: '', detail: 'echo ready' },
		{ kind: 'mcp_server', name: 'github', support: 'loaded', reason: '', detail: 'npx -y @modelcontextprotocol/server-github' },
		{ kind: 'mcp_server', name: 'docs', support: 'loaded', reason: '', detail: 'https://mcp.example.com/docs' },
	],
});
const agentInstall = plugin({
	id: 'deploy-helper@mp', name: 'deploy-helper', marketplace: 'mp', description: 'Ships builds', enabled: false,
	approval: 'pending', has_mcp: true,
	components: [
		{ kind: 'skill', name: 'ship', support: 'loaded', reason: '', detail: '' },
		{ kind: 'mcp_server', name: 'deployer', support: 'loaded', reason: '', detail: 'uvx deployer-mcp --region eu' },
		{ kind: 'service', name: 'cloudflare', support: 'loaded', reason: '', detail: '' },
	],
});
const imported = plugin({
	id: 'code-review@claude-plugins-official', name: 'code-review', marketplace: 'claude-plugins-official',
	description: 'Reviews pull requests', has_agents: true, has_commands: true,
	components: [
		{ kind: 'command', name: 'review', support: 'loaded', reason: '', detail: '' },
		{ kind: 'agent', name: 'reviewer', support: 'loaded', reason: 'model pin removed: the model is chosen per chat here; MultiEdit became Edit; TodoWrite was dropped: this app has no equivalent tool', detail: '' },
		{ kind: 'skill', name: 'checklist', support: 'loaded', reason: 'effort pin ignored: the model is chosen per chat here', detail: '' },
		{ kind: 'agent', name: 'notebook-runner', support: 'unsupported', reason: 'none of the tools it lists exist here, and an empty tool list would give it every tool, so it is not offered', detail: '' },
		{ kind: 'executables', name: 'bin/', support: 'unsupported', reason: "files in bin/ are not added to the shell's PATH here", detail: '' },
		{ kind: 'monitor', name: 'monitors', support: 'unsupported', reason: 'plugin monitors do not run here', detail: '' },
		{ kind: 'user_config', name: 'api_key', support: 'needs_config', reason: 'uses plugin settings this app does not collect (api_key)', detail: '' },
		{ kind: 'service', name: 'github', support: 'loaded', reason: '', detail: '' },
		{ kind: 'service', name: 'linear', support: 'loaded', reason: '', detail: '' },
	],
});
const marketplace = { id: 'openai-mp', source: 'git', url: 'https://github.com/acme/agent-plugins.git', install_location: '/x', last_updated: '2026-10-02T10:00:00',
	skipped: [{ name: 'npm-only-tool', reason: "packages from npm can't be installed here" }, { name: '../escape', reason: "its name can't be used as a folder name" }] };
const available = (p) => ({ ...p, marketplace_path: '/x', installed: false, enabled: false });

async function truth(viewport, touch) {
	const name = `${viewport.width}x${viewport.height}`;
	const check = (condition, label, detail) => {
		assert.ok(condition, `[${name}] ${label}${detail === undefined ? '' : `: ${JSON.stringify(detail)}`}`);
		passed++;
	};
	const context = await browser.newContext({ viewport, isMobile: touch, hasTouch: touch, deviceScaleFactor: 1, serviceWorkers: 'block' });
	let world = {};
	const sent = [];
	await context.route((u) => u.pathname.startsWith('/api/'), async (route) => {
		const request = route.request();
		const path = decodeURIComponent(new URL(request.url()).pathname.replace('/api/v1', ''));
		if (request.method() !== 'GET') sent.push({ method: request.method(), path, body: request.postData() ? request.postDataJSON() : null });
		if (path === '/auth/status') return route.fulfill({ json: { authenticated: true, setup_required: false, is_admin: true, username: 'owner' } });
		if (path === '/plugins/installed') return route.fulfill({ json: world.installed ?? [] });
		if (path === '/plugins/available') return route.fulfill({ json: world.available ?? [] });
		if (path === '/plugins/marketplaces') return route.fulfill({ json: world.marketplaces ?? [] });
		if (path === '/plugins/updates') return route.fulfill({ json: [] });
		if (path === '/plugins/auto-update/settings') return route.fulfill({ json: { enabled: false, interval_hours: 24, auto_sync_marketplaces: true, auto_update_plugins: false, last_check: null } });
		if (path === '/projects') return route.fulfill({ json: world.projects ?? [] });
		const project = path.match(/^\/projects\/([^/]+)\/plugins\/installed$/);
		if (project) return route.fulfill({ json: world.projectPlugins?.[project[1]] ?? [] });
		if (request.method() === 'POST' && path.endsWith('/enable')) return route.fulfill({ json: { status: 'enabled' } });
		if (path.startsWith('/connectors')) return route.fulfill({ json: [] });
		const found = [...(world.installed ?? []), ...(world.available ?? [])].find((p) => path === `/plugins/${p.id}`);
		if (found && request.method() === 'GET') return route.fulfill({ json: { ...details(found), scope: found.installed === false ? 'marketplace' : found.scope, readme: '# About\nA plugin.' } });
		return route.fulfill({ status: 404, json: { detail: 'not in this fixture' } });
	});
	const page = await context.newPage();
	const errors = [];
	page.on('pageerror', (e) => errors.push(e.message));
	const noOverflow = () => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
	const shot = async (file) => { if (shots) await page.screenshot({ path: join(shots, `${name}-truth-${file}.png`), fullPage: true }); };
	const open = async (state, item) => {
		world = state;
		await page.evaluate(() => sessionStorage.clear()).catch(() => {});
		await page.goto(`${base}?tab=plugins${item ? `&item=${encodeURIComponent(item)}` : ''}`);
	};
	const detailPane = () => page.getByRole('region', { name: 'Details' });

	// Empty: nothing installed, no sources.
	await open({}, 'explore');
	await page.getByText('Nothing here yet. Add a source.').waitFor();
	check(await page.getByText('Nothing added yet.').isVisible() || touch, 'empty list says nothing is added');
	check((await noOverflow()) <= 0, 'no overflow, empty', await noOverflow());
	await shot('1-empty');

	// A marketplace plugin with hooks and MCP servers: switching on shows what runs first.
	await open({ installed: [hooked] }, `plugin:${hooked.id}`);
	const sw = detailPane().getByRole('switch', { name: 'Enabled' });
	await sw.waitFor();
	await sw.click();
	const card = page.getByTestId('plugin-enable-review');
	await card.waitFor();
	const cardText = await card.innerText();
	check(cardText.includes('Before a tool runs') && cardText.includes('guard.sh'), 'review names the hook and its command', cardText);
	check(cardText.includes('Runs a program') && cardText.includes('server-github'), 'review names the program it starts', cardText);
	check(cardText.includes('Connects to a server'), 'review names the remote server', cardText);
	check(!sent.some((s) => s.path.endsWith('/enable')), 'nothing enabled before the confirm', sent);
	check((await noOverflow()) <= 0, 'no overflow, enable review', await noOverflow());
	await shot('2-enable-review');
	await card.getByRole('button', { name: 'Turn on' }).click();
	await card.waitFor({ state: 'detached' });
	check(sent.some((s) => s.path === `/plugins/${hooked.id}/enable`), 'Turn on enables it', sent);

	// An agent install waiting for the owner, next to one the owner switched off.
	await open({ installed: [{ ...hooked }, agentInstall] }, `plugin:${agentInstall.id}`);
	const review = page.getByTestId('plugin-review');
	await review.waitFor();
	const reviewText = await review.innerText();
	check(reviewText.includes('uvx deployer-mcp'), 'agent install review shows the command', reviewText);
	check(reviewText.includes('cloudflare'), 'agent install review shows the service it needs', reviewText);
	check(await page.getByText('Added by an agent. Review first.').isVisible(), 'agent install banner');
	check((await noOverflow()) <= 0, 'no overflow, agent install', await noOverflow());
	await shot('3-agent-install');
	// InstallPlugin's marketplace install (approval "pending", switched off):
	// listed under Needs your review, apart from a plugin the owner turned off,
	// with no switch, and approving it is a plain enable (no content hash).
	check(await detailPane().getByRole('switch', { name: 'Enabled' }).count() === 0, 'agent install has no switch until approved');
	if (!touch) {
		const groupOf = (id) => page.evaluate((id) => {
			let el = document.querySelector(`[data-settings-item="${id}"]`);
			while (el && !el.classList?.contains('st-lg')) el = el.previousElementSibling;
			return el?.textContent?.trim() ?? null;
		}, id);
		check((await groupOf(`plugin:${agentInstall.id}`)) === 'Needs your review', 'agent install under Needs your review', await groupOf(`plugin:${agentInstall.id}`));
		check((await groupOf(`plugin:${hooked.id}`)) !== 'Needs your review', 'owner-disabled plugin not under Needs your review', await groupOf(`plugin:${hooked.id}`));
	}
	await review.getByRole('button', { name: 'Approve and enable' }).click();
	await review.getByRole('button', { name: 'Approve', exact: true }).click();
	await page.waitForTimeout(300);
	const approval = sent.find((s) => s.path === `/plugins/${agentInstall.id}/enable`);
	check(!!approval && !approval.body?.content_hash, 'approving an agent install is a plain enable', approval);

	// An imported plugin: what was adapted, what is left out, outside services.
	await open({ installed: [imported] }, `plugin:${imported.id}`);
	const adapted = page.getByTestId('plugin-adapted');
	await adapted.waitFor();
	const adaptedText = await adapted.innerText();
	check(/model pin removed/i.test(adaptedText) && adaptedText.includes('MultiEdit became Edit'), 'adaptation notes shown', adaptedText);
	const leftText = await page.getByTestId('plugin-not-loaded').innerText();
	check(leftText.includes('Programs') && leftText.includes('Monitor') && !/executables|user_config/.test(leftText), 'left-out parts have readable kinds', leftText);
	check((await page.getByTestId('plugin-services').innerText()).includes('linear'), 'declared services listed');
	check((await noOverflow()) <= 0, 'no overflow, imported', await noOverflow());
	await shot('4-imported');

	// A source with skipped entries: Explore says which and why.
	await open({ installed: [], available: [available(plugin({ id: 'notes@openai-mp', name: 'notes', marketplace: 'openai-mp', description: 'Keeps notes' }))], marketplaces: [marketplace] }, 'explore');
	const skipped = page.getByTestId('plugins-skipped');
	await skipped.waitFor();
	const skippedText = await skipped.innerText();
	check(skippedText.includes('npm-only-tool') && /packages from npm can't be installed here/i.test(skippedText), 'skipped entry with reason', skippedText);
	check((await noOverflow()) <= 0, 'no overflow, explore', await noOverflow());
	await shot('5-skipped');
	await detailPane().getByRole('button', { name: 'Install notes' }).click();
	await page.waitForTimeout(300);
	const install = sent.find((s) => s.path === '/plugins/install');
	check(install?.body?.enable === false, 'Explore installs switched off', install);

	// Project-scope plugins.
	await open({
		projects: [{ id: 'web', name: 'Website' }, { id: 'api', name: 'API server' }],
		projectPlugins: { web: [plugin({ id: 'lint@project-local', name: 'lint', marketplace: 'project', scope: 'local' }), plugin({ id: 'brand@project-project', name: 'brand', marketplace: 'project', scope: 'project' })] },
	}, 'projects');
	await page.getByText("Chats don't load these yet.").waitFor();
	const projectsText = await detailPane().innerText();
	check(projectsText.includes('Website') && !projectsText.includes('API server'), 'only projects with plugins listed', projectsText);
	check(projectsText.includes('Not loaded'), 'project plugin says it does not load', projectsText);
	check((await noOverflow()) <= 0, 'no overflow, projects', await noOverflow());
	await shot('6-projects');

	// Add source: name follows the URL.
	await open({ marketplaces: [marketplace] }, 'sources');
	await detailPane().getByRole('button', { name: 'Add source' }).click();
	await detailPane().getByRole('textbox', { name: 'Git URL' }).fill('https://github.com/acme/team-plugins.git');
	check((await detailPane().getByRole('textbox', { name: 'Name' }).inputValue()) === 'team-plugins', 'name derived from the URL');
	check(await detailPane().getByRole('button', { name: 'Add', exact: true }).isEnabled(), 'Add is enabled with only a URL');
	check((await noOverflow()) <= 0, 'no overflow, sources', await noOverflow());
	await shot('7-sources');

	check(errors.length === 0, 'no page errors', errors);
	await context.close();
}

try {
	await harness({ width: 1280, height: 800 }, false);
	await harness({ width: 390, height: 844 }, true);
	await truth({ width: 1280, height: 800 }, false);
	await truth({ width: 390, height: 844 }, true);
	console.log(`settings-plugins: ${passed} checks passed`);
} finally {
	await browser.close();
}
