// Run while the isolated Vite fixture is serving on :5197.
// Optional: PLAYWRIGHT_MODULE=/path/to/playwright-core CHROMIUM_PATH=/path/to/chromium
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
const output = process.env.SCREENSHOT_DIR || '/tmp/composer-suggestion-browser';
await mkdir(output, { recursive: true });
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
		await page.route(
			url => url.pathname.startsWith('/api/'),
			route => route.abort()
		);
		page.on('pageerror', error => console.error(error.message));
		await page.goto(
			process.env.FIXTURE_URL || 'http://localhost:5197/tests/composer-suggestion.html'
		);
		await page.waitForFunction(() => !!window.suggestionFixture);
		const results = await page.evaluate(async () =>
			(await import('/tests/composer-suggestion.verify.js')).verifyComposerSuggestions()
		);
		console.log(JSON.stringify(results));
		const editor = page.locator('[contenteditable="true"]');
		const literal = await page.evaluate(() => window.suggestionFixture.literal);
		const reoffer = () =>
			page.evaluate(async () => {
				await window.suggestionFixture.clear();
				await window.suggestionFixture.offer();
			});
		const ghost = page.locator('[data-testid="composer-suggestion"]');
		// #1402: the suggestion is inline only; no separate action row.
		await reoffer();
		await ghost.waitFor();
		for (const name of [/use reply/i, /dismiss/i]) {
			assert.equal(await page.getByRole('button', { name }).count(), 0);
		}
		await editor.focus();
		await page.keyboard.press('Tab');
		assert.equal(await editor.textContent(), literal);
		assert.equal(await page.evaluate(() => window.suggestionFixture.sends), 0);
		await reoffer();
		await ghost.waitFor();
		await editor.focus();
		await page.keyboard.press('Enter');
		assert.notEqual(await editor.textContent(), literal);
		assert.equal(await page.evaluate(() => window.suggestionFixture.sends), 0);
		await reoffer();
		await ghost.waitFor();
		const box = await editor.boundingBox();
		if (width === 390) {
			// Touch users accept with a double tap; a single tap only focuses.
			const at = [box.x + box.width / 2, box.y + box.height / 2];
			await page.touchscreen.tap(...at);
			assert.equal(await editor.textContent(), '');
			await page.touchscreen.tap(...at);
		} else {
			await editor.dblclick();
		}
		await page.waitForFunction(
			text => document.querySelector('[contenteditable="true"]').textContent === text,
			literal
		);
		assert.equal(await page.evaluate(() => window.suggestionFixture.sends), 0);
		await reoffer();
		await ghost.waitFor();
		await editor.focus();
		await page.keyboard.type('My own reply');
		assert.equal(await page.locator('[data-testid="composer-suggestion"]').count(), 0);
		assert.equal(await editor.textContent(), 'My own reply');
		await reoffer();
		await ghost.waitFor();
		await page.screenshot({ path: `${output}/composer-${width}x${height}.png` });
		console.log(
			`Inline-only, Tab, double ${width === 390 ? 'tap' : 'click'} and typing passed; screenshot: ${output}/composer-${width}x${height}.png`
		);
		await page.close();
	}
} finally {
	await browser.close();
}
