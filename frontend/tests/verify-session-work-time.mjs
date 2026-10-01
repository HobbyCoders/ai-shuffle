// Real-browser check for #1619 (the sidebar card's total working time).
// Serve the fixture first:
//   npx vite --config tests/vite.session-work-time.config.js --port 5241 --strictPort
// then run with playwright-core from browser-mcp/ (npm ci there):
//   PLAYWRIGHT_MODULE=../browser-mcp/node_modules/playwright-core \
//   CHROMIUM_PATH=/path/to/chrome node tests/verify-session-work-time.mjs
import { createRequire } from 'node:module';
import { mkdir } from 'node:fs/promises';
import assert from 'node:assert/strict';

const require = createRequire(import.meta.url);
const { chromium } = require(
	process.env.PLAYWRIGHT_MODULE || '/opt/browser-mcp/node_modules/playwright-core'
);
const browser = await chromium.launch({
	headless: true,
	executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium',
	args: ['--no-sandbox']
});
const url = process.env.FIXTURE_URL || 'http://localhost:5241/tests/session-work-time.html';
const output = process.env.SCREENSHOT_DIR || '/tmp/session-work-time-browser';
await mkdir(output, { recursive: true });

/** "1h 02m" / "12m 34s" / "42s" -> seconds (minute precision for hours). */
function seconds(text) {
	const h = /(\d+)h/.exec(text);
	const m = /(\d+)m/.exec(text);
	const s = /(\d+)s/.exec(text);
	return (h ? +h[1] * 3600 : 0) + (m ? +m[1] * 60 : 0) + (s ? +s[1] : 0);
}

try {
	for (const [width, height] of [
		[1440, 900],
		[390, 844]
	]) {
		const page = await browser.newPage({
			viewport: { width, height },
			isMobile: width === 390,
			hasTouch: width === 390
		});
		const errors = [];
		page.on('pageerror', (error) => errors.push(error.message));
		await page.route(
			(u) => u.pathname.startsWith('/api/'),
			(route) => route.abort()
		);
		const ready = () => !!window.workFixture && !!window.workFixture.timer('a');
		await page.goto(url);
		try {
			await page.waitForFunction(ready, null, { timeout: 15_000 });
		} catch {
			// A cold Vite server re-optimizes deps on first load and reloads the
			// page out from under the fixture; one fresh load settles it.
			errors.length = 0;
			await page.reload();
			await page.waitForFunction(ready);
		}
		const timer = (id) => page.evaluate((i) => window.workFixture.timer(i), id);

		// Done card: the banked total, not the 2h since its last activity.
		assert.equal(await timer('a'), '12m 34s');
		// Parked card keeps its total; settled row keeps its age.
		assert.equal(await timer('b'), '1h 30m');
		assert.equal(await timer('c'), '2h');
		await page.screenshot({ path: `${output}/${width}-settled.png` });

		// A new turn starts 5 s ago: the card resumes from the banked total and ticks.
		await page.evaluate(() => window.workFixture.start('a', 5_000));
		const first = seconds(await timer('a'));
		// The shared ticker refreshes `now` every 250 ms, so allow a second of lag.
		assert.ok(first >= 754 + 4 && first <= 754 + 7, `started at ${first}`);
		await page.waitForTimeout(2_100);
		const ticked = seconds(await timer('a'));
		assert.ok(ticked >= first + 2, `ticked ${first} -> ${ticked}`);
		await page.screenshot({ path: `${output}/${width}-working.png` });

		// The turn parks: the banking push lands, and the total stops growing.
		await page.evaluate(() => window.workFixture.finish('a', 'parked', 754_000 + 9_000));
		const parked = await timer('a');
		assert.equal(parked, '12m 43s');
		await page.waitForTimeout(1_600);
		assert.equal(await timer('a'), parked, 'parked total must not tick');

		// A wake turn runs and completes: the total keeps growing from there.
		await page.evaluate(() => window.workFixture.start('a', 1_000));
		await page.waitForTimeout(1_100);
		await page.evaluate(() => window.workFixture.finish('a', 'done', 754_000 + 9_000 + 3_000));
		assert.equal(await timer('a'), '12m 46s');

		// Layout: the timer stays inside its card and the page never scrolls sideways.
		const layout = await page.evaluate(() => {
			const out = [];
			for (const id of ['a', 'b', 'c']) {
				const card = document.querySelector(`[data-row="${id}"] .scard`);
				const t = document.querySelector(`[data-row="${id}"] .timer`);
				const c = card.getBoundingClientRect();
				const r = t.getBoundingClientRect();
				out.push({ id, inside: r.left >= c.left && r.right <= c.right + 0.5, visible: r.width > 0 });
			}
			return {
				out,
				overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
				title: document.querySelector('[data-row="a"] .timer').getAttribute('title')
			};
		});
		for (const r of layout.out) assert.ok(r.inside && r.visible, `timer ${r.id} clipped`);
		assert.equal(layout.overflow, false, 'horizontal overflow');
		assert.equal(layout.title, 'Total working time');
		await page.screenshot({ path: `${output}/${width}-final.png` });
		assert.deepEqual(errors, []);
		console.log(`${width}x${height}: ok`);
		await page.close();
	}
} finally {
	await browser.close();
}
