// Real-browser check that the page banner's dismiss X (and its Undo) can be
// clicked: the archive Undo banner used to sit inside the desktop window's drag
// strip, where Electron hands every click to Windows as a title-bar click, so
// the X looked fine but never received the press.
//
// A plain Chromium page has no window drag regions, so the desktop scenarios
// assert the geometry Electron relies on: every banner control lies below the
// title-bar strip, clear of every element whose computed app-region is drag and
// of the native caption buttons. Each scenario then dismisses the banner by a
// CDP-dispatched mouse click, a touch tap, Enter and Space, and checks the hit
// target size (32px desktop, 44px phone shell and touch screens).
//
// Run from frontend/ while the fixture is serving:
//   npx vite dev --config tests/vite.banner-host.config.js --host 127.0.0.1 --port 5261 --strictPort
//   node tests/verify-banner-host.mjs
// Needs browser-mcp's playwright-core (npm ci --ignore-scripts --prefix ../browser-mcp)
// and a Chrome/Chromium binary (CHROMIUM_PATH). BANNER_HOST_FIXTURE_URL overrides
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
const base = process.env.BANNER_HOST_FIXTURE_URL || 'http://127.0.0.1:5261/tests/banner-host.html';
const shots = process.env.SCREENSHOT_DIR;
let passed = 0;

const scenarios = [
	{ name: 'desktop app 1440', query: '?desktop=1', viewport: { width: 1440, height: 900 }, minTarget: 32 },
	// The desktop window's minimum width: the banner nearly reaches the caption buttons.
	{ name: 'desktop app 900', query: '?desktop=1', viewport: { width: 900, height: 560 }, minTarget: 32 },
	{ name: 'web 1440', query: '', viewport: { width: 1440, height: 900 }, minTarget: 32 },
	{ name: 'web 390 touch', query: '', viewport: { width: 390, height: 844 }, touch: true, minTarget: 44 },
	{ name: 'phone shell 390', query: '?shell=mobile', viewport: { width: 390, height: 844 }, touch: true, minTarget: 44 },
	// A tablet in landscape: wider than the 640px breakpoint, still the phone shell.
	{ name: 'phone shell 820', query: '?shell=mobile', viewport: { width: 820, height: 600 }, minTarget: 44 },
];

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
	await page.locator('.banner').first().waitFor();
	// Single archive, then bulk archive and "archive all settled" stacked under it.
	await page.evaluate(() => {
		window.__banner.show('Archived 3 sessions');
		window.__banner.show('Archived 12 sessions');
	});
	await page.locator('.banner').nth(2).waitFor();
	if (shots) await page.screenshot({ path: join(shots, `banner-${s.name.replace(/\s+/g, '-')}.png`) });

	// Geometry of every banner control against the drag strip and caption buttons.
	const geo = await page.evaluate(() => {
		const box = (el) => { const r = el.getBoundingClientRect(); return { left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height }; };
		const region = (el) => { const cs = getComputedStyle(el); return cs.getPropertyValue('app-region') || cs.getPropertyValue('-webkit-app-region'); };
		const drags = [...document.querySelectorAll('body *')].filter((el) => region(el) === 'drag').map(box);
		const caption = document.querySelector('[data-testid="caption-buttons"]');
		const controls = [...document.querySelectorAll('.banner-close, .banner-action')].map((el) => {
			const r = box(el);
			// Every point of the visible control must land on the control itself.
			const probes = [];
			for (const fx of [0.15, 0.5, 0.85]) for (const fy of [0.15, 0.5, 0.85]) {
				const x = r.left + r.width * fx, y = r.top + r.height * fy;
				const hit = document.elementFromPoint(x, y);
				probes.push({ x, y, ok: !!hit && (hit === el || el.contains(hit)), hit: hit?.className?.baseVal ?? hit?.className });
			}
			const icon = el.querySelector('svg');
			return { cls: el.className, label: el.getAttribute('aria-label') ?? el.textContent.trim(), rect: r, icon: icon ? box(icon) : null, probes };
		});
		return { drags, caption: caption ? box(caption) : null, controls, overlayTop: getComputedStyle(document.documentElement).getPropertyValue('--desktop-overlay-top').trim(), dragRuleSeen: drags.length };
	});
	const intersects = (a, b) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
	if (s.query === '?desktop=1') check(geo.drags.length >= 2, 'the desktop drag strip is present in the fixture', geo.drags.length);
	check(geo.controls.length === 6, 'three banners each show Undo and a dismiss X', geo.controls.length);
	for (const c of geo.controls) {
		const tag = `${c.label} at ${Math.round(c.rect.left)},${Math.round(c.rect.top)}`;
		for (const d of geo.drags) check(!intersects(c.rect, d), `${tag} is clear of the window drag strip`, { control: c.rect, drag: d });
		if (geo.caption) check(!intersects(c.rect, geo.caption), `${tag} is clear of the caption buttons`, { control: c.rect, caption: geo.caption });
		for (const p of c.probes) check(p.ok, `${tag} receives a press at ${Math.round(p.x)},${Math.round(p.y)}`, p.hit);
		if (c.label === 'Dismiss') {
			check(c.rect.width >= s.minTarget && c.rect.height >= s.minTarget, `${tag} hit target is at least ${s.minTarget}px`, c.rect);
			check(c.icon && c.icon.left >= c.rect.left && c.icon.right <= c.rect.right && c.icon.top >= c.rect.top && c.icon.bottom <= c.rect.bottom, `${tag} visible X sits inside its hit target`, c);
		}
	}

	const count = () => page.locator('.banner').count();
	const closes = page.getByRole('button', { name: 'Dismiss' });
	check((await closes.first().getAttribute('title')) === 'Dismiss', 'the X has an accessible name and tooltip');

	// Mouse (or touch tap) at the X's centre dismisses that banner only.
	const first = await closes.first().boundingBox();
	if (s.touch) await page.touchscreen.tap(first.x + first.width / 2, first.y + first.height / 2);
	else await page.mouse.click(first.x + first.width / 2, first.y + first.height / 2);
	await page.waitForFunction(() => document.querySelectorAll('.banner').length === 2);
	check((await count()) === 2, 'a press on the X dismisses one banner');
	check(await page.evaluate(() => window.__banner.undo === 0), 'dismissing does not run Undo');

	// The edge of the hit target, not only the glyph, dismisses too.
	const edge = await closes.first().boundingBox();
	if (s.touch) await page.touchscreen.tap(edge.x + 3, edge.y + 3);
	else await page.mouse.click(edge.x + 3, edge.y + 3);
	await page.waitForFunction(() => document.querySelectorAll('.banner').length === 1);
	check((await count()) === 1, 'a press near the edge of the X target dismisses');

	// Keyboard: the X is reachable with Tab and closes on Enter and on Space.
	await page.evaluate(() => document.activeElement?.blur());
	await page.keyboard.press('Tab');
	await page.keyboard.press('Tab');
	check(await page.evaluate(() => document.activeElement?.getAttribute('aria-label') === 'Dismiss'), 'Tab reaches the X after Undo');
	check(await page.evaluate(() => getComputedStyle(document.activeElement).outlineStyle !== 'none'), 'the focused X shows a focus ring');
	await page.keyboard.press('Enter');
	await page.waitForFunction(() => document.querySelectorAll('.banner').length === 0);
	check((await count()) === 0, 'Enter on the X dismisses');
	await page.evaluate(() => window.__banner.show());
	await closes.first().focus();
	await page.keyboard.press('Space');
	await page.waitForFunction(() => document.querySelectorAll('.banner').length === 0);
	check((await count()) === 0, 'Space on the X dismisses');

	// Undo still works by pointer from where the banner now sits.
	await page.evaluate(() => window.__banner.show());
	const undo = await page.getByRole('button', { name: 'Undo' }).boundingBox();
	if (s.touch) await page.touchscreen.tap(undo.x + undo.width / 2, undo.y + undo.height / 2);
	else await page.mouse.click(undo.x + undo.width / 2, undo.y + undo.height / 2);
	await page.waitForFunction(() => window.__banner.undo === 1);
	check((await count()) === 0, 'Undo runs and removes the banner');

	check(errors.length === 0, 'no page errors', errors);
	await context.close();
	console.log(`ok  ${s.name}`);
}

await browser.close();
console.log(`banner host: ${passed} checks passed`);
