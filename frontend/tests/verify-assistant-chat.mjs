// Real-browser check of assistant mode in the chat view (desktop and phone
// shells) against a fixture with every request answered in the page.
//
// Run from frontend/:  node tests/verify-assistant-chat.mjs
// It starts the fixture's Vite server itself and stops it before exiting.
// Needs browser-mcp's playwright-core (npm ci --ignore-scripts --prefix ../browser-mcp)
// and a Chrome/Chromium binary (CHROMIUM_PATH). SCREENSHOT_DIR saves screenshots
// (default: a folder in the OS temp directory, never inside the repository).
import assert from 'node:assert/strict';
import { mkdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { createServer } from 'vite';

const require = createRequire(new URL('../../browser-mcp/package.json', import.meta.url));
const { chromium } = require('playwright-core');
const shots = process.env.SCREENSHOT_DIR || join(tmpdir(), 'assistant-chat-verify');
await mkdir(shots, { recursive: true });

const server = await createServer({
	configFile: fileURLToPath(new URL('./vite.assistant-chat.config.js', import.meta.url)),
	server: { host: '127.0.0.1', port: 0, strictPort: false },
	logLevel: 'error',
});
await server.listen();
const base = server.resolvedUrls.local[0].replace(/\/$/, '');
const browser = await chromium.launch({
	executablePath: process.env.CHROMIUM_PATH || {
		win32: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
		darwin: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
	}[process.platform] || '/usr/bin/chromium',
	headless: true,
	args: ['--no-sandbox', '--disable-gpu'],
});

const results = [];
function check(ok, label) {
	results.push(`${ok ? 'PASS' : 'FAIL'} ${label}`);
	if (!ok) process.exitCode = 1;
}

async function open(query, { width, height, mobile = false }) {
	const page = await browser.newPage({ viewport: { width, height }, isMobile: mobile, hasTouch: mobile });
	const errors = [];
	page.on('pageerror', (error) => errors.push(error.message));
	await page.goto(`${base}/tests/assistant-chat.html?${query}${mobile ? '&shell=mobile' : ''}`);
	await page.waitForFunction(() => window.assistantChat?.mounted === true, null, { timeout: 30_000 });
	await page.waitForSelector('[role="log"], .chat-view', { timeout: 30_000 });
	await page.waitForTimeout(600);
	if (mobile && await page.locator('.modal-overlay').count() === 0) {
		// The phone composer folds its controls row away until the box is tapped.
		await page.locator('[contenteditable="true"]').first().click();
		await page.waitForTimeout(300);
	}
	page.errors = errors;
	return page;
}
const count = (page, selector) => page.locator(selector).count();
const role = (page, name, kind = 'button') => page.getByRole(kind, { name }).count();
async function shot(page, name) {
	await page.screenshot({ path: join(shots, `${name}.png`) });
}
async function noOverflow(page, label) {
	const width = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
	check(width <= 0, `${label}: no sideways scroll`);
}

try {
	for (const viewport of [{ width: 1280, height: 800, tag: 'desktop' }, { width: 390, height: 844, mobile: true, tag: 'phone' }]) {
		const t = viewport.tag;

		// Home, idle: greeting, a bare composer, no header extras.
		let page = await open('chat=home&state=idle', viewport);
		check(await page.getByText('Hello, Sam.').count() === 1, `${t} home idle: greeting`);
		check(await count(page, '.context-pill-row') === 0, `${t} home idle: no agent/workspace/branch/runner chips`);
		check(await role(page, /^Permission mode:/) === 0, `${t} home idle: no permission or Away menu`);
		check(await role(page, /^Effort:/) === 0, `${t} home idle: no effort tag`);
		check(await role(page, /^Context and usage/) === 0, `${t} home idle: no context ring`);
		check(await role(page, 'Toggle live browser view') === 0, `${t} home idle: no browser toggle`);
		check(await role(page, 'Attach file') === 1, `${t} home idle: attach kept`);
		if (!viewport.mobile) check(await role(page, /^Model:/) === 1, `${t} home idle: model picker kept`);
		check(await count(page, '.work-strip') === 0, `${t} home idle: no work strip`);
		check(await page.locator('[contenteditable="true"]').count() === 1, `${t} home idle: composer present`);
		await noOverflow(page, `${t} home idle`);
		await shot(page, `${t}-home-idle`);
		check(page.errors.length === 0, `${t} home idle: no page errors ${page.errors.join(' | ')}`);
		await page.close();

		// Home with one job: strip line, Details collapsed, no tree.
		page = await open('chat=home&jobs=1', viewport);
		check(await role(page, 'Fixing the sidebar bug, 6 min') === 1, `${t} home 1 job: one plain strip line`);
		check(await role(page, 'Stop Fixing the sidebar bug') === 1, `${t} home 1 job: one Stop`);
		check(await count(page, '.active-tasks') === 0, `${t} home 1 job: no running-tasks tree`);
		const stripText = await page.locator('.work-strip').innerText();
		check(!/Frontend Designer|w1|opus|claude/i.test(stripText), `${t} home 1 job: no ids, agents or models in the strip`);
		check(await page.getByText('On it. One job is fixing').count() === 1, `${t} home: assistant words shown`);
		check(await page.getByText('The release checks all passed.').count() === 1, `${t} home: assistant report shown`);
		check(await page.getByText('All 41 checks passed on main.').count() === 0, `${t} home: raw started-chat report hidden`);
		const toggles = page.getByRole('button', { name: /^Details/ });
		// The started chat's raw report folds into the same Details as the work that started it.
		check(await toggles.count() === 1, `${t} home: one Details disclosure, none on a turn that only talked`);
		check(await toggles.first().getAttribute('aria-expanded') === 'false', `${t} home: Details collapsed by default`);
		check(await page.getByText('git log --oneline -5').count() === 0, `${t} home: tool rows hidden while collapsed`);
		check(await count(page, '.worklog') === 0, `${t} home: no work-log groups inline`);
		await noOverflow(page, `${t} home 1 job`);
		await toggles.first().scrollIntoViewIfNeeded();
		await shot(page, `${t}-home-1-job-details-collapsed`);
		await toggles.first().click();
		await page.waitForTimeout(250);
		check(await toggles.first().getAttribute('aria-expanded') === 'true', `${t} home: Details opens`);
		check(await page.locator('.details-body').first().innerText().then((s) => /git log/.test(s)), `${t} home: tool rows inside Details`);
		await toggles.first().scrollIntoViewIfNeeded();
		await shot(page, `${t}-home-1-job-details-expanded`);
		check(page.errors.length === 0, `${t} home 1 job: no page errors ${page.errors.join(' | ')}`);
		await page.close();

		// Home with five jobs: three lines and "and 2 more".
		page = await open('chat=home&jobs=5', viewport);
		check(await page.locator('.work-strip li').count() === 3, `${t} home 5 jobs: three lines`);
		check(await role(page, 'and 2 more') === 1, `${t} home 5 jobs: and 2 more`);
		await noOverflow(page, `${t} home 5 jobs`);
		await shot(page, `${t}-home-5-jobs`);
		await page.getByRole('button', { name: 'and 2 more' }).click();
		check(await page.locator('.work-strip li').count() === 5, `${t} home 5 jobs: expands to five`);
		check(await role(page, 'Waiting on checks for the release, 51 min') === 1 || await page.getByText('Waiting on checks for the release').count() === 1, `${t} home 5 jobs: started chat listed`);
		await shot(page, `${t}-home-5-jobs-expanded`);
		await page.close();

		// Home with a pending approval: a plain question, no dialog or buttons.
		page = await open('chat=home&ask=1', viewport);
		check(await count(page, '.modal-overlay') === 0, `${t} home ask: no approval dialog`);
		const asked = page.locator('.assistant-ask');
		check(await asked.count() === 1 && /Do you authorize me to run\s*git push origin main\s*\? Please answer yes or no\./.test(await asked.innerText()), `${t} home ask: asked as a plain message in the server wording`);
		check(await role(page, /^(Allow|Deny|Approve)/) === 0, `${t} home ask: no approve or deny buttons`);
		await shot(page, `${t}-home-ask`);
		await page.close();

		// A request without assistant_confirm keeps today's dialog, in the home too.
		page = await open('chat=home&ask=card', viewport);
		check(await count(page, '.modal-overlay') === 1, `${t} home unmarked request: dialog kept`);
		check(await count(page, '.assistant-ask') === 0, `${t} home unmarked request: not asked in words`);
		await page.close();

		// Ordinary chat: everything as before.
		page = await open('chat=ordinary&jobs=1', viewport);
		if (!viewport.mobile) {
			check(await count(page, '.context-pill-row') === 1, `${t} ordinary: context chips row kept`);
			check(await role(page, /^Permission mode:/) === 1, `${t} ordinary: permission menu kept`);
			check(await role(page, /^Effort:/) === 1, `${t} ordinary: effort kept`);
			check(await role(page, /^Context and usage/) >= 1, `${t} ordinary: context ring kept`);
		} else {
			check(await role(page, /^Context and usage/) >= 1, `${t} ordinary: context ring kept`);
		}
		check(await role(page, /Download private backup/) === 1, `${t} ordinary: transcript download kept`);
		check(await count(page, '.active-tasks') === 1, `${t} ordinary: running-tasks tree kept`);
		check(await count(page, '.work-strip') === 0, `${t} ordinary: no work strip`);
		check(await page.getByRole('button', { name: /^Details/ }).count() === 0, `${t} ordinary: no Details disclosures`);
		await shot(page, `${t}-ordinary`);
		check(page.errors.length === 0, `${t} ordinary: no page errors ${page.errors.join(' | ')}`);
		await page.close();
		page = await open('chat=ordinary&ask=card', viewport);
		check(await count(page, '.modal-overlay') === 1, `${t} ordinary: approval dialog kept`);
		await shot(page, `${t}-ordinary-approval`);
		await page.close();

		// Home header: no transcript download in the home.
		page = await open('chat=home&jobs=1', viewport);
		check(await role(page, /Download private backup/) === 0, `${t} home: no transcript download`);
		if (viewport.mobile) {
			await page.locator('.mobile-settings-trigger').first().click();
			await page.waitForTimeout(300);
			check(await role(page, /^Model:/) >= 1, `${t} home sheet: model picker kept`);
			check(await page.getByRole('toolbar', { name: 'Session context' }).count() === 0, `${t} home sheet: no session context`);
			check(await page.getByRole('group', { name: 'Reasoning effort' }).count() === 0, `${t} home sheet: no effort`);
			check(await page.getByRole('group', { name: 'Tool access' }).count() === 0, `${t} home sheet: no tool access`);
			await shot(page, `${t}-home-controls-sheet`);
		}
		await page.close();
	}
} finally {
	console.log(results.join(String.fromCharCode(10)));
	console.log(`screenshots: ${shots}`);
	await browser.close();
	await server.close();
}
assert.ok(results.length > 0);
