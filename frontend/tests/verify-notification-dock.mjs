// Real-browser check for the shared notification dock (#1823): every notice
// draws in one container just under the page header, centred on the chat
// column (not the window) with the sidebar open or closed and a side panel
// open, and every control takes a press exactly where it is drawn.
//
// A plain Chromium page has no window drag regions, so the desktop scenarios
// assert the geometry Electron relies on: every dock control lies clear of
// every element whose computed app-region is drag and of the native caption
// buttons, and the dock is after the top bar in document order (Electron
// applies drag and no-drag regions in that order, ignoring stacking).
//
// Run from frontend/ while the fixture is serving:
//   npx vite dev --config tests/vite.notification-dock.config.js --host 127.0.0.1 --port 5261 --strictPort
//   node tests/verify-notification-dock.mjs
// Needs browser-mcp's playwright-core (npm ci --ignore-scripts --prefix ../browser-mcp)
// and a Chrome/Chromium binary (CHROMIUM_PATH). NOTIFICATION_DOCK_FIXTURE_URL
// overrides the URL; SCREENSHOT_DIR saves a screenshot per scenario.
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
const base = process.env.NOTIFICATION_DOCK_FIXTURE_URL || 'http://127.0.0.1:5261/tests/notification-dock.html';
const shots = process.env.SCREENSHOT_DIR;
let passed = 0;

const scenarios = [
	{ name: 'desktop app 1440 sidebar open', query: '?desktop=1', viewport: { width: 1440, height: 900 }, minTarget: 32 },
	{ name: 'desktop app 1440 sidebar closed', query: '?desktop=1&sidebar=0', viewport: { width: 1440, height: 900 }, minTarget: 32 },
	{ name: 'desktop app 1440 side panel open', query: '?desktop=1&panel=1', viewport: { width: 1440, height: 900 }, minTarget: 32 },
	// The desktop window's minimum width.
	{ name: 'desktop app 900', query: '?desktop=1&sidebar=0', viewport: { width: 900, height: 560 }, minTarget: 32 },
	{ name: 'web 1440', query: '', viewport: { width: 1440, height: 900 }, minTarget: 32 },
	{ name: 'phone shell 390', query: '?shell=mobile', viewport: { width: 390, height: 844 }, touch: true, minTarget: 44 },
];

const measure = (page) => page.evaluate(() => {
	const box = (el) => { const r = el.getBoundingClientRect(); return { left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height }; };
	const region = (el) => { const cs = getComputedStyle(el); return cs.getPropertyValue('app-region') || cs.getPropertyValue('-webkit-app-region'); };
	const docks = [...document.querySelectorAll('[data-testid="notification-dock"]')];
	const dock = docks[0];
	const stack = dock?.querySelector('.dock-stack');
	const topbar = document.querySelector('.topbar');
	const surface = document.querySelector('.main-surface');
	const drags = [...document.querySelectorAll('body *')].filter((el) => region(el) === 'drag').map(box);
	const caption = document.querySelector('[data-testid="caption-buttons"]');
	const cards = [...(stack?.querySelectorAll(':scope > .dock-entry > *') ?? [])].map((el) => {
		const r = box(el);
		// Inside the card every probe lands on the card; just outside, none do.
		// Corners are rounded (up to 12px), so corner probes sit 14px in from the sides.
		const inside = [[14, 3], [r.width - 14, 3], [r.width / 2, r.height / 2], [14, r.height - 3], [r.width - 14, r.height - 3]]
			.map(([dx, dy]) => { const hit = document.elementFromPoint(r.left + dx, r.top + dy); return !!hit && el.contains(hit); });
		const above = document.elementFromPoint(r.left + r.width / 2, r.top - 2);
		const below = document.elementFromPoint(r.left + r.width / 2, r.bottom + 2);
		return { cls: String(el.className), rect: r, inside, outsideHits: [above, below].some((h) => h && el.contains(h)), animation: getComputedStyle(el).animationName };
	});
	const controls = [...(stack?.querySelectorAll('button') ?? [])].filter((el) => el.offsetParent).map((el) => {
		const r = box(el);
		const probes = [];
		for (const fx of [0.15, 0.5, 0.85]) for (const fy of [0.15, 0.5, 0.85]) {
			const hit = document.elementFromPoint(r.left + r.width * fx, r.top + r.height * fy);
			probes.push(!!hit && (hit === el || el.contains(hit)));
		}
		return { label: el.getAttribute('aria-label') ?? el.textContent.trim(), rect: r, probes, region: region(el) };
	});
	return {
		count: docks.length,
		variant: dock?.className ?? '',
		dockRegion: dock ? region(dock) : '',
		dock: dock ? box(dock) : null,
		stack: stack ? box(stack) : null,
		topbar: topbar ? box(topbar) : null,
		surface: surface ? box(surface) : null,
		afterTopbar: !!(topbar && dock && (topbar.compareDocumentPosition(dock) & Node.DOCUMENT_POSITION_FOLLOWING)),
		drags, caption: caption ? box(caption) : null, cards, controls,
	};
});

for (const s of scenarios) {
	const check = (condition, label, detail) => {
		assert.ok(condition, `[${s.name}] ${label}${detail === undefined ? '' : `: ${JSON.stringify(detail)}`}`);
		passed++;
	};
	const context = await browser.newContext({ viewport: s.viewport, isMobile: !!s.touch, hasTouch: !!s.touch, deviceScaleFactor: 1, serviceWorkers: 'block' });
	await context.route((u) => u.pathname.startsWith('/api/'), (route) => route.abort());
	const page = await context.newPage();
	const errors = [];
	page.on('pageerror', (e) => errors.push(e.message));
	await page.goto(base + s.query);
	await page.locator('.dock-entry').nth(2).waitFor();
	await page.evaluate(() => window.__dock.show('Archived 3 sessions'));
	await page.locator('.dock-entry').nth(3).waitFor();
	await page.waitForTimeout(250); // let the fade-in finish before probing
	if (shots) await page.screenshot({ path: join(shots, `dock-${s.name.replace(/\s+/g, '-')}.png`) });

	const geo = await measure(page);
	const intersects = (a, b) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
	check(geo.count === 1, 'exactly one dock is drawn (the root fallback stays hidden)', geo.count);
	check(geo.variant.includes('anchored'), 'the drawn dock is the one under the header', geo.variant);
	check(geo.dockRegion === 'no-drag', 'the dock is no-drag', geo.dockRegion);
	check(geo.afterTopbar, 'the dock follows the top bar in document order');
	check(geo.dock.top >= geo.topbar.bottom, 'the dock starts below the header', { dock: geo.dock.top, header: geo.topbar.bottom });
	check(geo.dock.top - geo.topbar.bottom <= 12, 'the dock sits just under the header', { dock: geo.dock.top, header: geo.topbar.bottom });
	const stackMid = (geo.stack.left + geo.stack.right) / 2, chatMid = (geo.surface.left + geo.surface.right) / 2;
	check(Math.abs(stackMid - chatMid) <= 1, 'the stack is centred on the chat column', { stackMid, chatMid });
	check(geo.stack.left >= geo.surface.left && geo.stack.right <= geo.surface.right, 'the stack stays inside the chat column', { stack: geo.stack, surface: geo.surface });
	check(geo.cards.length === 4, 'connection, computer use and two Undo banners all render in the dock', geo.cards.map((c) => c.cls));
	for (const c of geo.cards) {
		check(c.inside.every(Boolean), `${c.cls.split(' ')[0]} takes a press anywhere it is drawn`, c.inside);
		check(!c.outsideHits, `${c.cls.split(' ')[0]} takes no press outside its drawn box`);
	}
	for (const c of geo.controls) {
		const tag = `${c.label} at ${Math.round(c.rect.left)},${Math.round(c.rect.top)}`;
		for (const d of geo.drags) check(!intersects(c.rect, d), `${tag} is clear of the window drag strip`, { control: c.rect, drag: d });
		if (geo.caption) check(!intersects(c.rect, geo.caption), `${tag} is clear of the caption buttons`, { control: c.rect, caption: geo.caption });
		check(c.probes.every(Boolean), `${tag} receives a press over its whole box`, c.probes);
		if (c.label === 'Dismiss') check(c.rect.width >= s.minTarget && c.rect.height >= s.minTarget, `${tag} hit target is at least ${s.minTarget}px`, c.rect);
	}

	const tap = async (locator) => {
		const b = await locator.boundingBox();
		if (s.touch) await page.touchscreen.tap(b.x + b.width / 2, b.y + b.height / 2);
		else await page.mouse.click(b.x + b.width / 2, b.y + b.height / 2);
	};
	// Undo at its drawn centre runs and removes that banner only.
	await tap(page.getByRole('button', { name: 'Undo' }).first());
	await page.waitForFunction(() => window.__dock.undo === 1);
	check((await page.locator('.dock-entry').count()) === 3, 'Undo runs and removes one banner');
	// Reconnect, then Stop computer use, pressed where they are drawn.
	await tap(page.getByRole('button', { name: 'Reconnect' }));
	await page.waitForFunction(() => window.__dock.reconnect === 1);
	check(true, 'Reconnect responds at its drawn position');
	await tap(page.getByRole('button', { name: 'Stop computer use' }));
	await page.waitForSelector('[aria-label="Computer use stopped"]');
	check(true, 'Stop computer use responds at its drawn position');
	// Keyboard: Tab reaches a Dismiss and Enter closes it.
	await page.evaluate(() => document.activeElement?.blur());
	for (let i = 0; i < 40 && !(await page.evaluate(() => document.activeElement?.getAttribute('aria-label') === 'Dismiss')); i++) await page.keyboard.press('Tab');
	check(await page.evaluate(() => document.activeElement?.getAttribute('aria-label') === 'Dismiss'), 'Tab reaches a Dismiss');
	check(await page.evaluate(() => getComputedStyle(document.activeElement).outlineStyle !== 'none'), 'a focused Dismiss shows a focus ring');
	const before = await page.locator('.dock-entry').count();
	await page.keyboard.press('Enter');
	await page.waitForFunction((n) => document.querySelectorAll('.dock-entry').length === n - 1, before);
	check(true, 'Enter on Dismiss closes the banner');

	if (!s.touch) {
		// Opening and closing the sidebar and a side panel keeps the stack on the chat column.
		for (const [label, fn] of [['sidebar toggled', (o) => window.__dock.setSidebar(o)], ['side panel toggled', (o) => window.__dock.setPanel(o)]]) {
			for (const open of [true, false]) {
				await page.evaluate(`(${fn})(${open})`);
				await page.waitForTimeout(50);
				const g = await measure(page);
				const mid = (g.stack.left + g.stack.right) / 2, chat = (g.surface.left + g.surface.right) / 2;
				check(Math.abs(mid - chat) <= 1, `${label} (${open ? 'open' : 'closed'}): centred on the chat column`, { mid, chat });
			}
		}
	}

	check(errors.length === 0, 'no page errors', errors);
	await context.close();
	console.log(`ok  ${s.name}`);
}

// Reduced motion: cards appear without animating.
{
	const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, reducedMotion: 'reduce' });
	await context.route((u) => u.pathname.startsWith('/api/'), (route) => route.abort());
	const page = await context.newPage();
	await page.goto(base);
	await page.locator('.dock-entry').nth(2).waitFor();
	const g = await measure(page);
	assert.ok(g.cards.every((c) => c.animation === 'none'), `reduced motion: no card animation ${JSON.stringify(g.cards.map((c) => c.animation))}`);
	passed++;
	await context.close();
	console.log('ok  reduced motion');
}

await browser.close();
console.log(`notification dock: ${passed} checks passed`);
