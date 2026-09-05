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
		for (const key of ['Tab', 'Enter']) {
			await page.evaluate(async () => {
				await window.suggestionFixture.clear();
				await window.suggestionFixture.offer();
			});
			await editor.focus();
			await page.keyboard.press(key);
			assert.equal(
				await editor.textContent(),
				await page.evaluate(() => window.suggestionFixture.literal)
			);
			assert.equal(await page.evaluate(() => window.suggestionFixture.sends), 0);
		}
		await page.evaluate(async () => {
			await window.suggestionFixture.clear();
			await window.suggestionFixture.offer();
		});
		await editor.focus();
		await page.keyboard.type('My own reply');
		assert.equal(await page.locator('[data-testid="composer-suggestion"]').count(), 0);
		assert.equal(await editor.textContent(), 'My own reply');
		await page.evaluate(async () => {
			await window.suggestionFixture.clear();
			await window.suggestionFixture.offer();
		});
		await page.screenshot({ path: `${output}/composer-${width}x${height}.png` });
		console.log(
			`Native Tab/Enter and typing passed; screenshot: ${output}/composer-${width}x${height}.png`
		);
		await page.close();
	}
} finally {
	await browser.close();
}
