import assert from 'node:assert/strict';
import { verifyProviderLogin } from './verify-provider-login.mjs';
import { createRequire } from 'node:module';
const require = createRequire(
	new URL('../../browser-mcp/package.json', import.meta.url),
);
const { chromium } = require('playwright-core');
const browser = await chromium.launch({
	executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium',
	headless: true,
	args: ['--no-sandbox', '--disable-gpu'],
});
const url =
	process.env.BROWSER_FIXTURE_URL ||
	'http://127.0.0.1:5180/tests/browser-workspace.html';
try {
	for (const viewport of [
		{ width: 1440, height: 900 },
		{ width: 390, height: 844 },
	]) {
		const page = await browser.newPage({ viewport });
		const errors = [];
		page.on('pageerror', (e) => errors.push(e.message));
		await page.goto(url);
		await page
			.getByRole('button', { name: 'Take control', exact: true })
			.waitFor();
		assert.equal(
			await page
				.getByRole('button', { name: 'New tab', exact: true })
				.isDisabled(),
			true,
		);
		assert.deepEqual(
			await page.evaluate(() => window.browserFixture.messages),
			[],
		);
		await page
			.getByRole('button', { name: 'Take control', exact: true })
			.click();
		await page.getByRole('button', { name: 'New tab', exact: true }).click();
		await page.locator('canvas').waitFor();
		await page.evaluate(() => window.browserFixture.setWidth(640));
		await page.locator('summary').click();
		assert.deepEqual(
			await page.evaluate(() =>
				window.browserFixture.messages.map((m) => m.op),
			),
			['takeover', 'tabs'],
		);
		await page
			.getByLabel('Viewport preset', { exact: true })
			.selectOption('390x844');
		assert.deepEqual(
			await page.evaluate(() => window.browserFixture.state.viewport),
			{ mode: 'fixed', width: 390, height: 844 },
		);
		await page.getByRole('button', { name: 'Rotate viewport' }).click();
		assert.deepEqual(
			await page.evaluate(() => window.browserFixture.state.viewport),
			{ mode: 'fixed', width: 844, height: 390 },
		);
		await page.getByRole('button', { name: 'Rotate viewport' }).click();
		assert.equal(
			await page.evaluate(
				() => document.documentElement.scrollWidth > innerWidth,
			),
			false,
		);
		await page.waitForFunction(
			() =>
				document.querySelector('canvas')?.dataset.viewportWidth === '390' &&
				document.querySelector('canvas')?.dataset.viewportHeight === '844',
		);
		// Fit must settle even though every accepted command publishes workspace state.
		await page.getByLabel('Viewport preset', { exact: true }).selectOption('fit');
		await page.waitForTimeout(500);
		await page.locator('.address-display').click();
		const address = page.getByRole('textbox', { name: 'Page address' });
		await address.fill('https://draft.test/not-yet-sent');
		const beforeChurn = await page.evaluate(() => window.browserFixture.messages.filter(m => m.op === 'viewport').length);
		for (let i = 0; i < 12; i++) {
			await page.evaluate(i => window.browserFixture.churn(i), i);
			await page.waitForTimeout(125);
		}
		await page.waitForTimeout(400);
		const afterChurn = await page.evaluate(() => window.browserFixture.messages.filter(m => m.op === 'viewport').length);
		assert.equal(afterChurn, beforeChurn, 'identical fit viewport commands must stop during state/frame churn');
		assert.equal(await address.inputValue(), 'https://draft.test/not-yet-sent');
		assert.equal(await address.evaluate(el => el === document.activeElement), true);
		assert.equal(await page.evaluate(() => window.browserFixture.socketCount), 1);
		assert.equal(await page.evaluate(() => window.browserFixture.openSockets), 1);
		await address.press('Escape');
		// The observer still follows a genuinely changed body size exactly once.
		await page.setViewportSize({ width: viewport.width, height: viewport.height + 40 });
		await page.waitForTimeout(400);
		const resized = await page.evaluate(() => window.browserFixture.messages.filter(m => m.op === 'viewport'));
		assert.equal(resized.length, afterChurn + 1, 'a real fit resize sends one command');
		assert.notDeepEqual(resized.at(-1).args, resized.at(-2).args);
		await page.setViewportSize(viewport);
		await page.waitForTimeout(400);
		assert.equal(await page.evaluate(() => window.browserFixture.messages.filter(m => m.op === 'viewport').length), afterChurn + 2);
		await page.screenshot({
			path: `/tmp/browser-workspace-${viewport.width}.png`,
		});
		await page
			.getByRole('button', { name: 'Return control', exact: true })
			.click();
		assert.equal(
			await page
				.getByRole('button', { name: 'New tab', exact: true })
				.isDisabled(),
			true,
		);
		const count = await page.evaluate(
			() => window.browserFixture.messages.length,
		);
		await page.setViewportSize({
			width: viewport.width + 10,
			height: viewport.height,
		});
		assert.equal(
			await page.evaluate(() => window.browserFixture.messages.length),
			count,
		);
		await page.evaluate(() => window.browserFixture.setSession('another-fixture'));
		await page.waitForTimeout(300);
		assert.equal(await page.evaluate(() => window.browserFixture.socketCount), 2);
		assert.equal(await page.evaluate(() => window.browserFixture.openSockets), 1);
		assert.deepEqual(errors, []);
		console.log(
			JSON.stringify({
				viewport,
				overflow: false,
				firstTab: true,
				fixedResize: 'no commands',
				fitChurn: { before: beforeChurn, after: afterChurn, draftAndFocusPreserved: true, sockets: 1 },
				fitResize: 'one command per changed size',
				sessionSwitch: 'one replacement socket',
				presets: true,
				orientation: true,
				release: true,
				passiveResize: 'no commands',
				pageErrors: errors,
				screenshot: `/tmp/browser-workspace-${viewport.width}.png`,
			}),
		);
		await page.close();
		await verifyProviderLogin(browser, url, viewport);
	}
} finally {
	await browser.close();
}
