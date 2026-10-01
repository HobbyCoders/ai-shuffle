// Real-browser check for #1618: peer messages held while a parent was parked
// render as one attributed "Agent message" each, and no harness reminder text
// reaches the transcript. The fixture renders the production MessageArea over
// the real reducer's output for the real producer prompts.
//
// Run from frontend/ while the fixture is serving:
//   npx vite dev --config tests/vite.agent-delivery.config.js --host 127.0.0.1 --port 5291 --strictPort
//   node tests/verify-agent-delivery.mjs
// Needs browser-mcp's playwright-core (npm ci --ignore-scripts --prefix ../browser-mcp)
// and a Chrome/Chromium binary (CHROMIUM_PATH). AGENT_DELIVERY_FIXTURE_URL overrides the URL;
// AGENT_DELIVERY_SHOTS names a directory for screenshots.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { join } from 'node:path';
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
const url = process.env.AGENT_DELIVERY_FIXTURE_URL || 'http://127.0.0.1:5291/tests/agent-delivery.html';
// promotion 2 + unpark 2 + urgent 3 + idle 1
const EXPECTED = ['worker-a', 'worker-b', 'worker-a', 'worker-b', 'worker-a', 'worker-b', 'worker-c', 'worker-c'];

try {
	for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
		const name = `${viewport.width}x${viewport.height}`;
		const context = await browser.newContext({ viewport, deviceScaleFactor: 1, serviceWorkers: 'block' });
		const page = await context.newPage();
		const errors = [];
		page.on('pageerror', (error) => errors.push(String(error)));
		await page.goto(url);
		const log = page.locator('[role="log"]');
		await log.waitFor();
		await page.getByText('Thanks.').waitFor();
		const text = await log.innerText();
		for (const leak of ['system-reminder', 'Messages from other agents', '[agent message', '[/agent message]', 'held-peer-message']) {
			assert.ok(!text.includes(leak), `${name}: transcript shows ${leak}`);
		}
		// Each peer message is its own attributed tool-style card, in order.
		const cards = page.getByText('Agent message', { exact: true });
		assert.equal(await cards.count(), EXPECTED.length, `${name}: one card per peer message`);
		const order = [];
		for (const sender of ['worker-a', 'worker-b', 'worker-c']) {
			for (const box of await page.getByText(sender, { exact: true }).all()) {
				const rect = await box.boundingBox();
				if (rect) order.push([rect.y, sender]);
			}
		}
		assert.deepEqual(order.sort((a, b) => a[0] - b[0]).map(([, s]) => s), EXPECTED, `${name}: attribution and order`);
		// Opening a card shows that peer's own message, not the envelope.
		await cards.nth(1).click();
		await page.getByText('Tests are red:').first().waitFor();
		assert.ok(!(await log.innerText()).includes('[agent message'), `${name}: opened card shows the envelope`);
		// The human prompt is the only user bubble.
		assert.ok(text.includes('Split the investigation across two workers'), `${name}: human prompt shown`);
		const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
		assert.ok(overflow <= 0, `${name}: horizontal overflow ${overflow}px`);
		assert.deepEqual(errors, [], `${name}: page errors`);
		if (process.env.AGENT_DELIVERY_SHOTS) {
			await page.screenshot({ path: join(process.env.AGENT_DELIVERY_SHOTS, `agent-delivery-${name}.png`), fullPage: false });
			await log.evaluate((el) => {
				const scroller = el.querySelector('.msg-scroll') || el;
				scroller.scrollTop = 0;
			});
			await page.screenshot({ path: join(process.env.AGENT_DELIVERY_SHOTS, `agent-delivery-${name}-top.png`), fullPage: false });
		}
		console.log(`ok ${name}: ${EXPECTED.length} attributed agent messages, no reminder text`);
		await context.close();
	}
} finally {
	await browser.close();
}
