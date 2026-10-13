// Real-browser check for RunningTasksList (#1816) and the phone Git sheet.
// Layout rules the owner set for a 390px phone: title row with a compact Stop
// all, search on its own row, chips on their own row that never overflow or
// run together, names taking the free width with elapsed and worker on the
// right. Also: swipe left to stop, a swarm expanding, desktop 2 and 17 tasks,
// default dark and Vercel themes. Screenshots go to SHOTS_DIR (default tmp).
//
// Run from frontend/ while both fixtures are serving:
//   npx vite dev --config tests/vite.running-tasks.config.js --host 127.0.0.1 --port 5194 --strictPort
//   npx vite dev --config tests/vite.git-panel.config.js --host 127.0.0.1 --port 5193 --strictPort
//   node tests/verify-running-tasks.mjs
// Needs browser-mcp's playwright-core (npm ci --ignore-scripts --prefix ../browser-mcp).
import assert from 'node:assert/strict';
import { mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createRequire } from 'node:module';
const require = createRequire(new URL('../../browser-mcp/package.json', import.meta.url));
const { chromium } = require('playwright-core');
const browser = await chromium.launch({
	executablePath: process.env.CHROMIUM_PATH || {
		win32: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
		darwin: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
	}[process.platform] || '/usr/bin/chromium',
	headless: true,
	args: ['--no-sandbox', '--disable-gpu']
});
const base = process.env.RUNNING_TASKS_FIXTURE_URL || 'http://127.0.0.1:5194/tests/running-tasks.html';
const gitBase = process.env.GIT_PANEL_FIXTURE_URL || 'http://127.0.0.1:5193/tests/git-panel.html';
const shots = process.env.SHOTS_DIR || join(tmpdir(), 'running-tasks-shots');
mkdirSync(shots, { recursive: true });
let passed = 0;

async function open(url, viewport, touch) {
	const context = await browser.newContext({ viewport, isMobile: touch, hasTouch: touch, deviceScaleFactor: 1, serviceWorkers: 'block' });
	await context.route((u) => u.pathname.startsWith('/api/'), (route) => route.abort());
	const page = await context.newPage();
	const errors = [];
	page.on('pageerror', (e) => errors.push(e.message));
	await page.goto(url);
	await page.waitForTimeout(600);
	return { page, context, errors };
}

function checker(name) {
	return (condition, label, detail) => {
		assert.ok(condition, `[${name}] ${label}${detail === undefined ? '' : `: ${JSON.stringify(detail)}`}`);
		passed++;
	};
}

const rect = (page, selector) => page.locator(selector).first().evaluate((el) => {
	const r = el.getBoundingClientRect();
	return { top: r.top, bottom: r.bottom, left: r.left, right: r.right, width: r.width, height: r.height };
});

// Every row icon (agent, shell, swarm) takes the theme's primary colour; the
// glyph tells the kinds apart, never a fixed tool colour.
async function checkIconColours(page, check) {
	const seen = await page.evaluate(() => {
		const probe = document.createElement('span');
		probe.style.color = 'var(--primary)';
		document.body.appendChild(probe);
		const primary = getComputedStyle(probe).color;
		probe.remove();
		const icons = [...document.querySelectorAll('.row-icon')].filter((el) => el.getClientRects().length);
		const kinds = icons.map((el) => ({ kind: el.classList.contains('swarm') ? 'swarm' : [...el.classList].find((c) => c.startsWith('kind-')), color: getComputedStyle(el).color }));
		return { primary, kinds };
	});
	check(seen.kinds.some((k) => k.kind === 'kind-agent') && seen.kinds.some((k) => k.kind === 'kind-shell'), 'agent and shell icons both render', seen.kinds.length);
	const off = seen.kinds.filter((k) => k.color !== seen.primary);
	check(off.length === 0, 'every row icon follows the theme primary', { primary: seen.primary, off: off.slice(0, 3) });
}

async function phoneSheet(tasks, { theme = '', swarm = false } = {}) {
	const q = `?tasks=${tasks}&shell=mobile${swarm ? '&swarm=1' : ''}${theme ? `&theme=${theme}` : ''}`;
	const name = `phone ${tasks}${swarm ? ' swarm' : ''}${theme ? ` ${theme}` : ''}`;
	const check = checker(name);
	const { page, context, errors } = await open(base + q, { width: 390, height: 844 }, true);
	// Inline on the phone is one summary row.
	const summary = page.getByRole('button', { name: /running task/ });
	check(await summary.isVisible(), 'summary row shows');
	await page.screenshot({ path: join(shots, `${name.replace(/ /g, '-')}-inline.png`) });
	await summary.tap();
	await page.waitForTimeout(400);
	const dialog = page.getByRole('dialog', { name: 'Running tasks' });
	check(await dialog.isVisible(), 'sheet opens');
	await checkIconColours(page, check);
	const sheet = await rect(page, '.rt-sheet');
	check(Math.abs(sheet.width - 390) < 1 && Math.abs(sheet.height - 844) < 1, 'sheet is full height', sheet);
	// Title row, then search, then chips, each on its own row.
	const top = await rect(page, '.rt-top');
	const search = await rect(page, '.rt-search');
	const chips = await rect(page, '.rt-chips');
	check(search.top >= top.bottom - 1, 'search sits below the title row', { top, search });
	check(chips.top >= search.bottom - 1, 'chips sit below the search', { search, chips });
	check(chips.right <= 390 && chips.left >= 0, 'chip row fits the screen', chips);
	// Chips never overflow and label and count never touch.
	const chipInfo = await page.locator('.chip').evaluateAll((els) => els.map((el) => {
		const label = el.querySelector('.chip-label').getBoundingClientRect();
		const count = el.querySelector('.chip-count').getBoundingClientRect();
		const box = el.getBoundingClientRect();
		return { overflow: el.scrollWidth > el.clientWidth, gap: count.left - label.right, inside: label.left >= box.left && count.right <= box.right + 0.5, labelClipped: el.querySelector('.chip-label').scrollWidth > el.querySelector('.chip-label').clientWidth, h: box.height };
	}));
	for (const c of chipInfo) {
		check(!c.overflow && c.inside && !c.labelClipped, 'chip content fits', c);
		check(c.gap >= 4, 'chip label and count are apart', c);
	}
	// Stop all is compact next to the title, with a 44px hit area.
	const stopAll = await rect(page, '.stop-all');
	check(stopAll.height <= 34 && stopAll.width <= 96, 'Stop all is compact', stopAll);
	const title = await rect(page, '.rt-title h2');
	check(stopAll.left > title.right, 'Stop all does not crowd the title', { title, stopAll });
	// Names take the free width: the name cell runs up to the meta column.
	const rows = await page.locator('.rt-list li.row').evaluateAll((els) => els.slice(0, 8).map((el) => {
		const name = el.querySelector('.row-name').getBoundingClientRect();
		const text = el.querySelector('.name-text');
		const meta = el.querySelector('.row-meta').getBoundingClientRect();
		const stop = el.querySelector('.row-actions').getBoundingClientRect();
		return {
			name: text.textContent,
			nameRight: name.right,
			metaLeft: meta.left,
			metaRight: meta.right,
			actionsLeft: stop.left,
			truncated: text.scrollWidth > text.clientWidth,
			freeWidth: name.width - Math.min(text.scrollWidth, name.width),
			nameWidth: name.width
		};
	}));
	for (const r of rows) {
		check(r.metaLeft - r.nameRight <= 12, 'name cell reaches the meta column', r);
		check(r.actionsLeft - r.metaRight <= 12, 'meta is right-aligned beside the actions', r);
		check(!r.truncated || r.nameWidth > 150, 'a truncated name has had the free width', r);
	}
	// 44px targets.
	const small = await page.locator('.rt-sheet button:visible').evaluateAll((els) => els
		.filter((el) => !el.classList.contains('stop-all'))
		.map((el) => { const r = el.getBoundingClientRect(); return { label: el.getAttribute('aria-label') || el.textContent.trim(), w: r.width, h: r.height }; })
		.filter((b) => b.h < 38 || b.w < 38));
	check(small.length === 0, 'controls are touch-sized', small);
	const counts = await page.locator('.rt-title p').textContent();
	check(!/\b1 (agents|shell tasks|tasks)\b/.test(counts), 'counts pluralise', counts);
	await page.screenshot({ path: join(shots, `${name.replace(/ /g, '-')}-sheet.png`) });

	if (swarm) {
		await page.getByRole('button', { name: /^Swarm Release 0\.8\.24/ }).tap();
		await page.waitForTimeout(250);
		check(await page.getByRole('list', { name: /Tasks in Release/ }).isVisible(), 'swarm expands');
		await page.screenshot({ path: join(shots, `${name.replace(/ /g, '-')}-swarm.png`) });
		await page.getByText('Finished swarms').tap();
		await page.waitForTimeout(200);
		await page.locator('.finished').scrollIntoViewIfNeeded();
		await page.screenshot({ path: join(shots, `${name.replace(/ /g, '-')}-finished.png`) });
	}

	// Swipe the first running row left to stop it.
	const row = page.locator('.rt-list li.row[data-state="running"]:not([data-row^="swarm:"]) .row-inner').first();
	const label = await row.locator('.name-text').textContent();
	await row.scrollIntoViewIfNeeded();
	await page.waitForTimeout(200);
	const box = await row.boundingBox();
	const cdp = await context.newCDPSession(page);
	const y = box.y + box.height / 2;
	const x0 = box.x + box.width - 60;
	await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: x0, y }] });
	for (let i = 1; i <= 12; i++) {
		await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: x0 - i * 22, y }] });
		await page.waitForTimeout(16);
	}
	await page.screenshot({ path: join(shots, `${name.replace(/ /g, '-')}-swipe.png`) });
	await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
	await page.waitForTimeout(900);
	const stopped = await page.evaluate(() => window.runningTasksFixture.stopped.slice());
	check(stopped.length === 1, 'swipe left stops the row', { stopped, label });
	const after = page.locator('.rt-list li.row[data-state="stopped"]').first();
	check((await after.locator('.meta-time').textContent()) === 'Stopped', 'stopped row reads Stopped');
	check(((await after.locator('.meta-worker').textContent()) || '').length > 0, 'stopped row names the worker');
	await page.screenshot({ path: join(shots, `${name.replace(/ /g, '-')}-stopped.png`) });

	// Stop all asks first, naming the count.
	await page.locator('.stop-all').tap();
	await page.waitForTimeout(150);
	const question = await page.getByRole('alertdialog').textContent();
	check(/Stop \d+ tasks?\?/.test(question), 'Stop all confirm names the count', question);
	await page.screenshot({ path: join(shots, `${name.replace(/ /g, '-')}-confirm.png`) });
	check(errors.length === 0, 'no page errors', errors);
	await context.close();
}

async function desktop(tasks, { theme = '', swarm = false } = {}) {
	const q = `?tasks=${tasks}${swarm ? '&swarm=1' : ''}${theme ? `&theme=${theme}` : ''}`;
	const name = `desktop ${tasks}${swarm ? ' swarm' : ''}${theme ? ` ${theme}` : ''}`;
	const check = checker(name);
	const { page, context, errors } = await open(base + q, { width: 1280, height: 800 }, false);
	check(await page.getByTestId('tasks-toggle').isVisible(), 'inline toggle shows');
	const capped = await page.locator('.inline-body.capped').count();
	check(tasks > 6 ? capped === 1 : capped === 0, 'long list caps its height', { tasks, capped });
	await checkIconColours(page, check);
	if (swarm) {
		await page.getByRole('button', { name: /^Swarm Release 0\.8\.24/ }).click();
		await page.waitForTimeout(200);
	}
	await page.screenshot({ path: join(shots, `${name.replace(/ /g, '-')}-inline.png`) });
	const transcript = page.getByRole('button', { name: /^Open transcript:/ }).first();
	await transcript.click();
	const opened = await page.evaluate(() => window.runningTasksFixture.opened.slice());
	check(opened.length === 1 && opened[0].startsWith('child-'), 'Open transcript opens the subagent chat', opened);
	await page.getByRole('button', { name: 'Open as sheet (controls panel row)' }).click();
	await page.waitForTimeout(300);
	const sheet = await rect(page, '.rt-sheet');
	check(sheet.width <= 560 && sheet.height <= 720, 'desktop sheet is a centred panel', sheet);
	await page.screenshot({ path: join(shots, `${name.replace(/ /g, '-')}-sheet.png`) });
	await page.keyboard.press('Escape');
	await page.waitForTimeout(150);
	check((await page.getByRole('dialog').count()) === 0, 'Escape closes the sheet');
	check(errors.length === 0, 'no page errors', errors);
	await context.close();
}

async function gitSheet(theme) {
	const name = `git sheet phone${theme ? ` ${theme}` : ''}`;
	const check = checker(name);
	const { page, context, errors } = await open(`${gitBase}?shell=mobile`, { width: 390, height: 844 }, true);
	await page.evaluate(() => window.gitPanelFixture?.scenario?.('dirty'));
	await page.waitForTimeout(150);
	await page.getByRole('button', { name: /^Git actions:/ }).tap();
	await page.waitForTimeout(400);
	const panel = await rect(page, '.git-panel');
	check(panel.left === 0 && Math.abs(panel.right - 390) < 1 && panel.bottom <= 844.5, 'Git panel is a bottom sheet across the phone', panel);
	const text = await page.locator('.git-panel').textContent();
	check(!/swarm|Private access|transcript/i.test(text), 'Git sheet is Git only', text.slice(0, 200));
	await page.screenshot({ path: join(shots, `${name.replace(/ /g, '-')}.png`) });
	check(errors.length === 0, 'no page errors', errors);
	await context.close();
}

await phoneSheet(2);
await phoneSheet(17);
await phoneSheet(17, { swarm: true });
await phoneSheet(17, { theme: 'vercel' });
await phoneSheet(2, { theme: 'vercel', swarm: true });
await desktop(2);
await desktop(17);
await desktop(17, { swarm: true });
await desktop(17, { theme: 'vercel', swarm: true });
await gitSheet('');
await browser.close();
console.log(`verify-running-tasks: ${passed} checks passed. Screenshots in ${shots}`);
