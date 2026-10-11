// Real-browser check of the context meter on a provider model (#2274): a known
// bound (server-resolved or from the curated entry) shows its percentage, and an
// unknown one shows the tokens used with no percentage and no fill.
//
// Run from frontend/ while the fixture is serving:
//   npx vite dev --config tests/vite.context-meter.config.js --host 127.0.0.1 --port 5293 --strictPort
//   CHROMIUM_PATH=/usr/bin/chromium node tests/verify-context-meter.mjs [screenshot-dir]
// Needs browser-mcp's playwright-core (npm ci --prefix ../browser-mcp).
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
const require = createRequire(new URL('../../browser-mcp/package.json', import.meta.url));
const { chromium } = require('playwright-core');
const browser = await chromium.launch({
	executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium',
	headless: true,
	args: ['--no-sandbox', '--disable-gpu'],
});
const base = process.env.CONTEXT_METER_FIXTURE_URL || 'http://127.0.0.1:5293/tests/context-meter.html';
const shots = process.argv[2];
if (shots) mkdirSync(shots, { recursive: true });
let passed = 0;

const cases = {
	unknown: { label: 'Context and usage — 84.2k tokens used, context window unknown', header: /84\.2k used\s*· window unknown/, fill: false },
	server: { label: 'Context and usage — 64% of context window used', header: /84\.2k \/ 131\.1k\s*\(64%\)/, fill: true },
	catalog: { label: 'Context and usage — 33% of context window used', header: /84\.2k \/ 256k\s*\(33%\)/, fill: true },
};

for (const viewport of [{ width: 1280, height: 800 }, { width: 390, height: 844 }]) {
	const touch = viewport.width < 600;
	for (const [bound, want] of Object.entries(cases)) {
		const name = `${bound}-${viewport.width}x${viewport.height}`;
		const context = await browser.newContext({ viewport, isMobile: touch, hasTouch: touch, deviceScaleFactor: 1, serviceWorkers: 'block' });
		const page = await context.newPage();
		const errors = [];
		page.on('pageerror', (e) => errors.push(e.message));
		const check = (ok, what, detail) => { assert.ok(ok, `[${name}] ${what}${detail === undefined ? '' : `: ${JSON.stringify(detail)}`}`); passed++; };
		await page.goto(`${base}?bound=${bound}`);
		const pill = page.locator('.usage-indicator').first();
		await pill.waitFor({ timeout: 20000 });
		await page.waitForFunction((l) => document.querySelector('.usage-indicator')?.getAttribute('aria-label') === l, want.label, { timeout: 10000 })
			.catch(() => {});
		check((await pill.getAttribute('aria-label')) === want.label, 'pill aria-label', await pill.getAttribute('aria-label'));
		check(await pill.isVisible(), 'meter is mounted and visible for the provider session');
		check((await page.locator('.usage-ring .ring-progress').count()) === (want.fill ? 1 : 0), 'ring fill only for a known bound');
		if (shots) await page.screenshot({ path: join(shots, `context-meter-${name}-pill.png`) });
		await (touch ? pill.tap() : pill.click());
		await page.waitForTimeout(250);
		const header = ((await page.locator('.ctx-value').textContent()) ?? '').replace(/\s+/g, ' ');
		check(want.header.test(header), 'popover header', header);
		check((await page.locator('.ctx-segbar').count()) === (want.fill ? 1 : 0), 'segmented bar only for a known bound');
		if (!want.fill) {
			const hint = (await page.locator('.ctx-unknown').textContent()) ?? '';
			check(hint.includes('Settings → Providers'), 'unknown hint names where to set the window', hint);
			check((await page.locator('.cat-pct').count()) === 0, 'no per-row percentages');
		}
		const overflow = await page.evaluate(() => {
			const vw = document.documentElement.clientWidth;
			return [...document.querySelectorAll('.usage-popover *')].filter((e) => {
				const b = e.getBoundingClientRect();
				return b.width && (b.right > vw + 0.5 || b.left < -0.5);
			}).length;
		});
		check(overflow === 0, 'popover fits the viewport', overflow);
		if (shots) await page.screenshot({ path: join(shots, `context-meter-${name}-popover.png`) });
		check(errors.length === 0, 'no page errors', errors);
		await context.close();
	}
}
await browser.close();
console.log(`context meter: ${passed} checks passed`);
