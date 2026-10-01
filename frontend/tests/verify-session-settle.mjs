// Real-browser check for #1621: settling a session from its sidebar card goes
// through the real appData store and API client; a refusal must reach the
// notification list with the server's reason, and a success must add nothing.
//
// Run from frontend/ while the fixture is serving:
//   npx vite dev --config tests/vite.session-settle.config.js --host 127.0.0.1 --port 5263 --strictPort
//   node tests/verify-session-settle.mjs
// Needs browser-mcp's playwright-core (npm ci --ignore-scripts --prefix ../browser-mcp)
// and a Chrome/Chromium binary (CHROMIUM_PATH). SESSION_SETTLE_FIXTURE_URL overrides the URL.
import assert from 'node:assert/strict';
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
const url = process.env.SESSION_SETTLE_FIXTURE_URL || 'http://127.0.0.1:5263/tests/session-settle.html';
const OFFLINE = "DesktopWorker, the worker that runs this session, can't be reached right now. Try again once it is back online.";

async function run(viewport, touch) {
	const context = await browser.newContext({ viewport, isMobile: touch, hasTouch: touch, deviceScaleFactor: 1, serviceWorkers: 'block' });
	const page = await context.newPage();
	const errors = [];
	page.on('pageerror', (e) => errors.push(e.message));
	await page.goto(url);
	await page.getByTestId('membership').waitFor();
	const name = `${viewport.width}x${viewport.height}`;
	const cdp = touch ? await context.newCDPSession(page) : null;
	const settle = async () => {
		const card = page.locator('.scard').first();
		if (touch) {
			// A long swipe left across the row commits its one action on release.
			const box = await card.boundingBox();
			const y = box.y + box.height / 2;
			const point = (x) => [{ x, y, id: 1 }];
			await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: point(box.x + box.width - 20) });
			for (let step = 1; step <= 12; step++) {
				await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: point(box.x + box.width - 20 - step * 22) });
				await page.waitForTimeout(16);
			}
			await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
		} else {
			await card.hover();
			await page.getByTitle(/Settle session|Move to Active/).first().click();
		}
		await page.waitForTimeout(400);
	};
	const items = () => page.getByTestId('notifications').locator('li').allTextContents();
	const membership = () => page.getByTestId('membership').textContent();

	await page.evaluate((detail) => { window.settleReply = { status: 503, detail }; }, OFFLINE);
	await settle();
	assert.deepEqual(await items(), [`Failed to update session: ${OFFLINE}`], `[${name}] refusal names the offline owner`);
	assert.equal(await membership(), 'Active', `[${name}] a refused settle rolls back`);

	await page.evaluate(() => { window.settleReply = { status: 200 }; });
	await settle();
	assert.equal((await items()).length, 1, `[${name}] a successful settle adds no notification`);
	assert.equal(await membership(), 'Settled', `[${name}] a successful settle moves the row`);
	assert.deepEqual(errors, [], `[${name}] no page errors`);
	await page.screenshot({ path: process.env.SHOT_DIR ? `${process.env.SHOT_DIR}/settle-${name}.png` : undefined });
	await context.close();
	console.log(`ok ${name}`);
}

try {
	await run({ width: 1440, height: 900 }, false);
	await run({ width: 390, height: 844 }, true);
} finally {
	await browser.close();
}
