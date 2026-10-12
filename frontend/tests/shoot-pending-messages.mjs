// Screenshots of PendingMessages in every unsend state (#2275): no status,
// "Cancelling…" and "Already sent", plus an earlier chip cancelling under a
// pending last chip, and a pending chip behind an already-sent tail (which must
// keep the only enabled Interrupt) — at desktop (1280x800) and phone (390x844), dark and light.
//
// Run from frontend/ while the fixture is serving:
//   npx vite dev --config tests/vite.pending-messages.config.js --host 127.0.0.1 --port 6923 --strictPort
//   SCREENSHOT_DIR=/tmp/shots node tests/shoot-pending-messages.mjs
// Needs browser-mcp's playwright-core (npm ci --ignore-scripts --prefix ../browser-mcp)
// and a Chrome/Chromium binary (CHROMIUM_PATH).
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { createRequire } from 'node:module';
const require = createRequire(new URL('../../browser-mcp/package.json', import.meta.url));
const { chromium } = require('playwright-core');
const shots = process.env.SCREENSHOT_DIR;
if (!shots) throw new Error('Set SCREENSHOT_DIR');
mkdirSync(shots, { recursive: true });
const base = process.env.PENDING_FIXTURE_URL || 'http://127.0.0.1:6923/tests/pending-messages.html';
const browser = await chromium.launch({
	executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium',
	headless: true,
	args: ['--no-sandbox', '--disable-gpu'],
});
const viewports = [['desktop', { width: 1280, height: 800 }, false], ['phone', { width: 390, height: 844 }, true]];
let failures = 0;
for (const mode of (process.env.THEMES || 'dark,light').split(',')) {
	for (const [vpName, viewport, touch] of viewports) {
		const context = await browser.newContext({ viewport, isMobile: touch, hasTouch: touch, deviceScaleFactor: 1, reducedMotion: 'reduce' });
		const page = await context.newPage();
		const errors = [];
		page.on('pageerror', (e) => errors.push(e.message));
		await page.goto(`${base}?theme=${mode}`);
		await page.waitForSelector('.pending-messages');
		await page.waitForTimeout(400);
		// Hover the earlier chip in the "Cancelling" group so its disabled × shows on desktop.
		if (!touch) await page.locator('section').nth(1).locator('.bubble').first().hover();
		const tag = `${vpName}-${mode}`;
		await page.screenshot({ path: join(shots, `pending-${tag}.png`), fullPage: true });
		const facts = await page.evaluate(() => ({
			statuses: [...document.querySelectorAll('.pending-status')].map((n) => n.textContent),
			disabled: [...document.querySelectorAll('button:disabled')].map((n) => n.className.split(' ')[0]),
			enabled: document.querySelectorAll('button:not(:disabled)').length,
			overflow: document.documentElement.scrollWidth > window.innerWidth,
			interrupts: [...document.querySelectorAll('section')].map((sec) =>
				[...sec.querySelectorAll('.pending-action')].map((b) => (b.disabled ? 'disabled' : 'enabled')).join(',')),
			// WCAG contrast of "Already sent" against the page, resolved through a
			// canvas so color-mix()/oklch() come back as sRGB bytes.
			contrast: (() => {
				const ctx = Object.assign(document.createElement('canvas'), { width: 1, height: 1 }).getContext('2d');
				const rgb = (c) => { ctx.clearRect(0, 0, 1, 1); ctx.fillStyle = c; ctx.fillRect(0, 0, 1, 1); return [...ctx.getImageData(0, 0, 1, 1).data].slice(0, 3); };
				const lum = (c) => { const [r, g, b] = rgb(c).map((v) => { v /= 255; return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }); return 0.2126 * r + 0.7152 * g + 0.0722 * b; };
				const fg = lum(getComputedStyle(document.querySelector('.pending-status.is-delivered')).color);
				const bg = lum(getComputedStyle(document.body).backgroundColor);
				return Math.round(((Math.max(fg, bg) + 0.05) / (Math.min(fg, bg) + 0.05)) * 100) / 100;
			})(),
		}));
		console.log(tag, JSON.stringify(facts));
		if (errors.length || facts.overflow || facts.contrast < 4.5) { failures++; console.error(tag, errors, facts.overflow ? 'horizontal overflow' : '', facts.contrast < 4.5 ? `contrast ${facts.contrast}` : ''); }
		await context.close();
	}
}
await browser.close();
if (failures) process.exit(1);
