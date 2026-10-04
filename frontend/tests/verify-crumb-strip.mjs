// Real-browser layout check of the desktop strip's breadcrumb (#1799): a
// subagent transcript opened from the assistant shows three crumbs,
// "Sessions > Assistant > <title>". Crumbs must use the free space before they
// truncate, "Sessions" is never shortened while space is free, no crumb keeps
// empty space beside its text, and the current title takes what is left.
//
// Run from frontend/ while the fixture is serving:
//   npx vite dev --config tests/vite.crumb-strip.config.js --host 127.0.0.1 --port 5249 --strictPort
//   node tests/verify-crumb-strip.mjs
// Needs browser-mcp's playwright-core (npm ci --ignore-scripts --prefix ../browser-mcp,
// or PLAYWRIGHT_MODULE) and a Chrome/Chromium binary (CHROMIUM_PATH).
// CRUMB_STRIP_FIXTURE_URL overrides the URL; SCREENSHOT_DIR saves a screenshot per case.
import assert from 'node:assert/strict';
import { join } from 'node:path';
import { createRequire } from 'node:module';
const require = createRequire(new URL('../../browser-mcp/package.json', import.meta.url));
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright-core');
const browser = await chromium.launch({
	executablePath: process.env.CHROMIUM_PATH || {
		win32: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
		darwin: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
	}[process.platform] || '/usr/bin/chromium',
	headless: true,
	args: ['--no-sandbox', '--disable-gpu'],
});
const base = process.env.CRUMB_STRIP_FIXTURE_URL || 'http://127.0.0.1:5249/tests/crumb-strip.html';
const shots = process.env.SCREENSHOT_DIR;
let passed = 0;

const sessions = {
	child: { id: 'child', title: 'Agents page header review', hidden: true, parent_session_id: 'assistant' },
	assistant: { id: 'assistant', title: 'Assistant' },
};

async function measure({ width, actions, title }) {
	const context = await browser.newContext({ viewport: { width, height: 300 }, deviceScaleFactor: 1, serviceWorkers: 'block' });
	await context.route((u) => u.pathname.startsWith('/api/'), (route) => {
		const id = decodeURIComponent(new URL(route.request().url()).pathname.split('/').pop() || '');
		return sessions[id] ? route.fulfill({ json: sessions[id] }) : route.fulfill({ status: 404, json: {} });
	});
	const page = await context.newPage();
	const errors = [];
	page.on('pageerror', (e) => errors.push(e.message));
	const query = new URLSearchParams({ actions: String(actions), ...(title ? { title } : {}) });
	await page.goto(`${base}?${query}`);
	await page.locator('.crumb.parent', { hasText: 'Assistant' }).waitFor();
	await page.evaluate(() => document.fonts.ready);
	const name = `${width}px, actions ${actions}px${title ? `, "${title}"` : ''}`;
	if (shots) await page.locator('.topbar').screenshot({ path: join(shots, `crumb-strip-${width}-${actions}-${(title || 'default').length}.png`) });
	const m = await page.evaluate(() => {
		const textWidth = (el) => { const r = document.createRange(); r.selectNodeContents(el); return r.getBoundingClientRect().width; };
		const box = (el) => el.getBoundingClientRect();
		const crumb = (el) => ({ text: el.textContent.trim(), width: box(el).width, text_width: textWidth(el), truncated: el.scrollWidth > el.clientWidth + 1 });
		const nav = document.querySelector('nav.crumbs');
		const titleText = nav.querySelector('.title-text');
		return {
			order: [...nav.querySelectorAll('.crumb.project, .crumb.parent, .title-text')].map((el) => el.textContent.trim()),
			project: crumb(nav.querySelector('.crumb.project')),
			parent: crumb(nav.querySelector('.crumb.parent')),
			title: { ...crumb(titleText), width: box(titleText).width },
			// Space between the last crumb and the action cluster.
			free: box(document.querySelector('.tb-right')).left - box(nav.querySelector('.current')).right,
		};
	});
	await context.close();
	assert.deepEqual(errors, [], `[${name}] page errors`);
	return { name, ...m };
}

function check(condition, label, detail) {
	assert.ok(condition, `${label}${detail === undefined ? '' : `: ${JSON.stringify(detail)}`}`);
	passed++;
}

const LONG = 'Agents page header review and breadcrumb truncation in the desktop strip';
// The owner's window is about 1040px, with the chat's action cluster on the
// right; wider windows must behave the same way with room to spare.
for (const width of [1040, 1280])
	for (const actions of [330, 260])
		for (const title of [undefined, 'Hi', LONG]) {
			const m = await measure({ width, actions, title });
			const expected = ['Sessions', 'Assistant', title ?? 'Agents page header review'];
			check(m.order.join(' > ') === expected.join(' > '), `[${m.name}] three crumbs in order`, m.order);
			check(!m.project.truncated, `[${m.name}] "Sessions" is not shortened`, m.project);
			check(!m.parent.truncated, `[${m.name}] "Assistant" is not shortened`, m.parent);
			for (const c of [m.project, m.parent, m.title])
				if (!c.truncated) check(c.width - c.text_width < 2, `[${m.name}] no empty space beside "${c.text}"`, c);
			// Crumbs use the free space before they truncate; whatever is left
			// goes to the title, so a truncated title leaves no gap in the strip.
			if (m.free > 24) check(!m.title.truncated, `[${m.name}] nothing is cut while space is free`, m);
			if (m.title.truncated) check(m.free <= 24, `[${m.name}] a truncated title takes what is left`, m);
		}

await browser.close();
console.log(`verify-crumb-strip: ${passed} checks passed`);
