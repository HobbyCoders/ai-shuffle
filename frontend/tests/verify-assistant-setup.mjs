// Real-browser check of the minimal Assistant setup (issue #1608): name and
// avatar only; the agent behind the assistant is built in. Background research has no owner controls (no
// switch, pause, usage or limits); only the per-source opt-in remains. The
// owner's details are detected by the server, shown read-only and
// correctable, with Gmail/GitHub suggestion cards when those are missing. The
// owner's spending and job time limits sit in their own Limits section. A
// fake /api/v1/assistant backend lives in this script, so nothing reaches a
// server or a model provider.
//
// Run from frontend/ while the fixture is serving:
//   npx vite dev --config tests/vite.assistant-setup.config.js --host 127.0.0.1 --port 5741 --strictPort
//   node tests/verify-assistant-setup.mjs
// Needs browser-mcp's playwright-core (npm ci --ignore-scripts --prefix ../browser-mcp)
// and a Chrome/Chromium binary (CHROMIUM_PATH). ASSISTANT_FIXTURE_URL overrides
// the URL and SCREENSHOT_DIR saves a screenshot per viewport.
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
const url = process.env.ASSISTANT_FIXTURE_URL || 'http://127.0.0.1:5741/tests/assistant-setup.html';
// Servers before #1650 still return these legacy fields; the page must not show them.
const LEGACY = {
	max_runs_per_day: 12, max_concurrent_runs: 1, max_turns: 3, timeout_seconds: 180, max_cost_per_run: 1, max_cost_per_day: 5,
	paused: false, cost_accounting: 'reported_after_run_not_hard_cap', usage: { runs_today: 7, reported_cost_today: 0.42, in_flight_runs: 1 },
};
let passed = 0;

function fakeBackend() {
	const identities = [
		{ id: 'researcher', name: 'Researcher', avatar_id: 'robot', dedicated: false },
		{ id: 'reviewer', name: 'Reviewer', avatar_id: 'pixel', dedicated: false },
	];
	const state = {
		...LEGACY, configured: false, agent_id: null, home_session_id: null, enabled: false, research_sources: 0,
		status: 'unconfigured', agent: null, assistant_name: null, avatar_id: 'robot', avatar_presets: [],
		introduction: {}, proactive_guard_ready: true,
	};
	// What a detection pass on the server found (no Gmail, gh signed out).
	const detected = { preferred_name: 'ada', projects: ['Shuffle', 'Notes'] };
	const corrections = {};
	const detectRequests = [];
	const introduce = () => {
		const merged = { ...detected };
		for (const [key, value] of Object.entries(corrections)) merged[key] = value;
		Object.assign(state, {
			introduction: merged, introduction_detected: { ...detected },
			introduction_corrected: Object.keys(corrections).sort(), suggested_connections: ['gmail', 'github'],
		});
	};
	const sources = [{
		id: 'gmail', connector_id: 'gmail', name: 'Work Gmail', type: 'gmail', enabled: false, available: true, status: 'connected',
		trigger_ids: ['new_email'], observation_mode: 'poll_and_events', last_observed_at: null, last_error: null,
	}];
	const derive = () => {
		state.research_sources = sources.filter((s) => s.enabled).length;
		state.status = !state.configured ? 'unconfigured' : state.research_sources ? 'ready' : 'no_sources';
		state.enabled = state.status === 'ready';
	};
	const patches = [];
	// The owner's spending and job time limits (assistant_limits on the server).
	const limits = {
		spend_limit_enabled: true, daily_spend_limit_usd: 20, job_limit_enabled: true, max_job_minutes: 120,
		spent_today_usd: 3.1, day: '2026-10-03', timezone: 'UTC', resets_at: '2026-10-04T00:00:00+00:00',
		limit_reached: false, warning: false,
	};
	const limitPatches = [];
	function configure(patch) {
		patches.push(patch);
		if (patch.introduction) {
			for (const [key, value] of Object.entries(patch.introduction)) {
				if (value === '' || (Array.isArray(value) && !value.length)) delete corrections[key];
				else corrections[key] = value;
			}
			introduce();
			return;
		}
		let id = patch.agent_id;
		// The server runs the assistant on its built-in agent; the page sends none.
		if (patch.create_agent || (!id && !state.agent_id)) {
			const existing = identities.find((a) => a.dedicated);
			id = existing?.id ?? 'assistant';
			if (!existing) identities.push({ id, name: 'Assistant', avatar_id: 'robot', dedicated: true });
		}
		if (id) state.agent_id = id;
		const agent = identities.find((a) => a.id === state.agent_id);
		if (patch.assistant_name) agent.name = patch.assistant_name;
		if (patch.avatar_id) agent.avatar_id = patch.avatar_id;
		Object.assign(state, { configured: true, agent: { ...agent }, assistant_name: agent.name, avatar_id: agent.avatar_id });
		introduce();
		derive();
	}
	async function handle(route) {
		const request = route.request();
		const path = new URL(request.url()).pathname.replace('/api/v1', '');
		const json = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
		if (path === '/assistant' && request.method() === 'GET') {
			if (new URL(request.url()).searchParams.get('detect') === 'true') detectRequests.push(request.url());
			return json(state);
		}
		if (path === '/assistant' && request.method() === 'PATCH') { configure(request.postDataJSON()); return json(state); }
		if (path === '/assistant/agents') return json(identities);
		if (path === '/assistant/sources') return json(sources);
		if (path.startsWith('/assistant/sources/') && request.method() === 'PATCH') {
			const source = sources.find((s) => s.id === decodeURIComponent(path.split('/').pop()));
			source.enabled = request.postDataJSON().enabled;
			derive();
			return json(source);
		}
		if (path === '/assistant/activity') return json([]);
		// One worker on the account: the Run work on row stays hidden.
		if (path === '/assistant/worker') return json({ device_id: null, workers: [] });
		if (path === '/assistant/limits' && request.method() === 'GET') return json(limits);
		if (path === '/assistant/limits' && request.method() === 'PATCH') {
			const patch = request.postDataJSON();
			limitPatches.push(patch);
			if (patch.spend_limit_enabled === false && patch.confirm_spend_limit_off !== true) return json({ detail: 'Confirm first' }, 409);
			const { confirm_spend_limit_off: _confirmed, ...changes } = patch;
			Object.assign(limits, changes);
			return json(limits);
		}
		return json({ detail: `unexpected ${request.method()} ${path}` }, 404);
	}
	return { handle, patches, state, detectRequests, limitPatches };
}

async function run(viewport) {
	const name = `${viewport.width}x${viewport.height}`;
	const touch = viewport.width < 768;
	const context = await browser.newContext({ viewport, isMobile: touch, hasTouch: touch, deviceScaleFactor: 1, serviceWorkers: 'block' });
	const backend = fakeBackend();
	await context.route((u) => u.pathname.startsWith('/api/'), backend.handle);
	const page = await context.newPage();
	const errors = [];
	page.on('pageerror', (e) => errors.push(e.message));
	page.on('response', (r) => { if (r.status() >= 400) errors.push(`${r.status()} ${r.url()}`); });
	const check = (condition, label, detail) => {
		assert.ok(condition, `[${name}] ${label}${detail === undefined ? '' : `: ${JSON.stringify(detail)}`}`);
		passed++;
	};
	const press = async (locator) => { await (touch ? locator.tap() : locator.click()); await page.waitForTimeout(80); };
	const overflow = () => page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
	// The app shell scrolls inside the viewport, so a full-page shot needs a tall one.
	const shoot = async (label) => {
		if (!process.env.SCREENSHOT_DIR) return;
		await page.setViewportSize({ width: viewport.width, height: 2600 });
		await page.waitForTimeout(400);
		await page.screenshot({ path: join(process.env.SCREENSHOT_DIR, `assistant-${label}-${name}.png`) });
		await page.setViewportSize(viewport);
	};

	await page.goto(url);
	const nameInput = page.getByRole('textbox', { name: 'Assistant name' });
	await nameInput.waitFor();
	check(await nameInput.inputValue() === 'Assistant', 'name defaults to Assistant');
	check(await page.getByRole('radio').count() === 0, 'no agent-kind choice');
	check(await page.getByRole('combobox').count() === 0, 'no agent picker');
	check(await page.getByRole('button', { name: /Manage agents/ }).count() === 0, 'no Manage agents link');
	const avatars = page.getByRole('button', { name: / avatar$/ });
	check(await avatars.count() === 16, '16 avatar presets', await avatars.count());
	const pressed = page.locator('.avatar-picker button[aria-pressed="true"]');
	check(await pressed.count() === 1, 'one preset avatar preselected');
	const boxes = await avatars.evaluateAll((els) => els.map((el) => el.getBoundingClientRect()).map((r) => ({ x: Math.round(r.x), y: Math.round(r.y), w: r.width, h: r.height })));
	const rows = [...new Set(boxes.map((b) => b.y))];
	check(rows.length === 4 && boxes.slice(0, 4).every((b) => b.y === boxes[0].y), 'avatars form a 4x4 grid', rows);
	check(boxes.every((b) => b.w >= 44 && b.h >= 44), 'avatar targets at least 44px', boxes[0]);
	// Each avatar is its own SVG file, inlined and styled by AgentAvatar.
	const drawings = await page.locator('.avatar-picker .agent-avatar').evaluateAll((els) => els.map((el) => {
		const shape = el.querySelector('svg .shape');
		return { avatar: el.getAttribute('data-avatar'), fill: shape ? getComputedStyle(shape).fill : null, markup: el.innerHTML.length };
	}));
	check(new Set(drawings.map((d) => d.avatar)).size === 16, 'every picker button draws its own avatar', drawings.map((d) => d.avatar));
	check(drawings.every((d) => d.fill && d.fill !== 'none' && d.fill !== 'rgb(0, 0, 0)'), 'avatar shapes take the theme fill', drawings.map((d) => d.fill));
	check(await page.getByRole('textbox', { name: 'What should I call you?' }).count() === 0, 'no introduction form');
	check(await page.getByRole('spinbutton').count() === 0, 'no limit fields');
	check(await page.getByRole('checkbox').count() === 0, 'no background research switch');
	check(!(await page.locator('body').textContent()).match(/background research|12 passes|admission limit|Per run/i), 'no research toggle text or limit numbers');
	check(await overflow() <= 0, 'no horizontal overflow on setup', await overflow());
	await shoot('setup');

	await nameInput.fill('Juniper');
	await press(page.getByRole('button', { name: 'fox avatar' }));
	check(await page.getByRole('button', { name: 'fox avatar' }).getAttribute('aria-pressed') === 'true', 'avatar choice registers');
	await press(page.getByRole('button', { name: 'Create assistant' }));
	await page.getByRole('button', { name: 'Open conversation' }).waitFor();
	check(backend.patches.length === 1, 'one save request', backend.patches);
	assert.deepEqual(backend.patches[0], { assistant_name: 'Juniper', avatar_id: 'fox' }, `[${name}] save payload`);
	passed++;
	check(await page.getByRole('heading', { level: 1 }).textContent() === 'Juniper', 'home shows the new name');
	check(await page.getByText(/Assistant ready/).isVisible(), 'creation confirmed');
	check(await page.getByRole('button', { name: /Open agent settings/ }).count() === 0, 'no agent settings link');
	check(await overflow() <= 0, 'no horizontal overflow on home', await overflow());
	for (const label of [/Pause/, /Resume/, /Review & enable/]) {
		check(await page.getByRole('button', { name: label }).count() === 0, `no ${label.source} button`);
	}
	const homeText = () => page.locator('body').textContent();
	check(!(await homeText()).match(/runs · last 24 hours|in flight|hard billing cap|fixed limits|\$0\.42|\b12\b/), 'no usage panel, cost or limit numbers', (await homeText()).slice(0, 400));
	check(await page.getByText('No connected sources in use.').isVisible(), 'neutral line before any opt-in');
	// Limits: today's spend against the owner's limit, two fields, a confirmed off switch.
	const limitsSection = page.locator('section', { has: page.getByRole('heading', { name: 'Limits' }) });
	await limitsSection.getByText('of $20.00 spent today').waitFor();
	check((await limitsSection.innerText()).includes('$3.10'), "today's spend shown", await limitsSection.innerText());
	check(await limitsSection.getByRole('spinbutton', { name: 'Daily spending limit' }).inputValue() === '20', 'daily limit field');
	check(await limitsSection.getByRole('combobox', { name: 'Longest background job' }).inputValue() === '120', 'job time field');
	check(await page.getByRole('combobox', { name: 'Run work on' }).count() === 0, 'no work worker row with one worker');
	for (const helper of ['All work, at API prices', 'Applies to each job']) {
		check(await limitsSection.getByText(helper).isVisible(), `helper "${helper}"`);
	}
	const limitTargets = await limitsSection.locator('input,select,[role=switch]').evaluateAll((els) => els.map((el) => el.getBoundingClientRect().height));
	check(limitTargets.every((h) => h >= 42), 'limit controls are touch sized', limitTargets);
	await press(limitsSection.getByRole('switch', { name: 'Daily spending limit' }));
	check(await limitsSection.getByText('Turn off the daily spending limit?').isVisible(), 'switching the limit off asks first');
	check(backend.limitPatches.length === 0, 'nothing saved before confirming', backend.limitPatches);
	check(await overflow() <= 0, 'no horizontal overflow while confirming', await overflow());
	await shoot('limits-confirm');
	await press(limitsSection.getByRole('button', { name: 'Turn off limit' }));
	await limitsSection.getByText('spent today · no daily limit').waitFor();
	assert.deepEqual(backend.limitPatches.at(-1), { spend_limit_enabled: false, confirm_spend_limit_off: true }, `[${name}] off payload`);
	passed++;
	await press(limitsSection.getByRole('switch', { name: 'Daily spending limit' }));
	await limitsSection.getByText('of $20.00 spent today').waitFor();
	assert.deepEqual(backend.limitPatches.at(-1), { spend_limit_enabled: true }, `[${name}] on payload`);
	passed++;
	check(await overflow() <= 0, 'no horizontal overflow on limits', await overflow());
	// The per-source opt-in is data-access consent and stays.
	const toggle = page.getByRole('switch', { name: 'Observe Work Gmail' });
	check(await toggle.getAttribute('aria-checked') === 'false', 'source opt-in offered and off');
	await press(toggle);
	await page.waitForFunction(() => document.querySelector('[role=switch]')?.getAttribute('aria-checked') === 'true');
	check(await page.getByText('Using 1 connected source.').isVisible(), 'one neutral line once a source is opted in');
	check(await overflow() <= 0, 'no horizontal overflow after opting in', await overflow());

	// About you: detected details, read-only, correctable; suggestion cards.
	await page.waitForFunction(() => document.body.textContent.includes('What your assistant knows'));
	const about = page.locator('section', { has: page.getByRole('heading', { name: 'What your assistant knows' }) });
	const aboutText = async () => (await about.innerText()).replace(/\s+/g, ' ');
	check(backend.detectRequests.length >= 1, 'the page asked the server to re-detect', backend.detectRequests);
	check((await aboutText()).includes('Name ada') && (await aboutText()).includes('Shuffle, Notes'), 'detected name and projects shown', await aboutText());
	check((await aboutText()).includes('Email Not found'), 'missing email says so', await aboutText());
	check(await about.locator('input').count() === 0, 'details are read-only until Correct');
	const gmail = page.getByRole('button', { name: 'Connect Gmail' });
	const github = page.getByRole('button', { name: 'Sign in to GitHub' });
	check(await gmail.isVisible() && await github.isVisible(), 'Gmail and GitHub suggestion cards shown');
	await press(gmail);
	await press(github);
	const opened = await page.evaluate(() => window.assistantFixture.opened);
	assert.deepEqual(opened.slice(-2), [['plugins', 'connection:gmail'], ['settings', 'category=integrations&item=github']], `[${name}] suggestion links`);
	passed++;
	await shoot('home');
	await press(about.getByRole('button', { name: /Correct/ }));
	const nameField = about.getByRole('textbox', { name: 'Name', exact: true });
	check(await nameField.inputValue() === '' && await nameField.getAttribute('placeholder') === 'ada', 'correction starts empty over the detected value');
	await nameField.fill('Ada Lovelace');
	check(await overflow() <= 0, 'no horizontal overflow while correcting', await overflow());
	await shoot('correct');
	await press(about.getByRole('button', { name: 'Save' }));
	await page.waitForFunction(() => document.body.textContent.includes('Ada Lovelace· corrected'));
	assert.deepEqual(backend.patches.at(-1), { introduction: { preferred_name: 'Ada Lovelace', email: '', github_username: '', repository_refs: [] } }, `[${name}] correction payload`);
	passed++;
	check(await about.locator('input').count() === 0, 'back to read-only after saving');
	check((await page.getByRole('heading', { level: 1 }).locator('..').textContent()).includes('Hello, Ada Lovelace.'), 'greeting uses the corrected name');

	// Editing changes only the name and face; no agent fields are ever sent.
	await press(page.getByRole('button', { name: 'Edit assistant' }));
	check(await page.getByRole('radio').count() === 0, 'edit offers no agent choice');
	check(await nameInput.inputValue() === 'Juniper', 'edit starts from the saved name');
	await press(page.getByRole('button', { name: 'Save assistant' }));
	await page.getByRole('button', { name: 'Open conversation' }).waitFor();
	const resave = backend.patches.at(-1);
	check(!('agent_id' in resave) && !('create_agent' in resave), 'second save sends no agent fields', resave);
	check(errors.length === 0, 'no page errors or failed requests', errors);
	await context.close();
}

try {
	for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) await run(viewport);
	console.log(`assistant setup: ${passed} checks passed`);
} finally {
	await browser.close();
}
