import assert from 'node:assert/strict';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { verifyProviderLogin } from './verify-provider-login.mjs';
import { verifyColdStart } from './verify-browser-cold-start.mjs';
import { createRequire } from 'node:module';
const require = createRequire(
	new URL('../../browser-mcp/package.json', import.meta.url),
);
const { chromium } = require('playwright-core');
const browser = await chromium.launch({
	executablePath: process.env.CHROMIUM_PATH || (process.platform === 'win32' ? 'C:/Program Files/Google/Chrome/Application/chrome.exe' : '/usr/bin/chromium'),
	headless: true,
	args: ['--no-sandbox', '--disable-gpu'],
});
const newPage = browser.newPage.bind(browser);
browser.newPage = async options => { const page = await newPage(options); page.on('response', response => { if (response.status() >= 400) console.error('Fixture resource failure', response.status(), response.url()); }); return page; };
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
			false,
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
		await page.evaluate(() => { window.originalCanvas = document.querySelector('canvas'); });
		await page.getByRole('button', { name: 'New tab', exact: true }).click();
		await page.waitForTimeout(150);
		assert.equal(await page.evaluate(() => window.originalCanvas === document.querySelector('canvas')), true, 'target switch must preserve canvas');
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
		await page.locator('summary').click();
		await page.getByRole('button', { name: 'Show keyboard', exact: true }).click();
		await page.waitForFunction(() => document.activeElement?.tagName === 'TEXTAREA');
		await page.evaluate(() => {
			window.sampledCanvas = document.querySelector('canvas');
			window.frameSample = { clears: 0, draws: 0, removals: 0 };
			const ctx = window.sampledCanvas.getContext('2d');
			const clear = ctx.clearRect.bind(ctx), draw = ctx.drawImage.bind(ctx);
			ctx.clearRect = (...args) => { window.frameSample.clears++; return clear(...args); };
			ctx.drawImage = (...args) => { window.frameSample.draws++; return draw(...args); };
			window.canvasObserver = new MutationObserver(() => { if (!window.sampledCanvas.isConnected) window.frameSample.removals++; });
			window.canvasObserver.observe(document.querySelector('.panel-body'), { childList: true, subtree: true });
		});
		for (let i = 0; i < 4; i++) { await page.evaluate(i => window.browserFixture.churn(i), i); await page.waitForTimeout(60); }
		assert.equal(await page.evaluate(() => document.activeElement?.tagName), 'TEXTAREA', 'same-target updates retain keyboard focus');
		assert.equal(await page.evaluate(() => document.querySelector('canvas') === window.sampledCanvas), true);
		const sample = await page.evaluate(() => { window.canvasObserver.disconnect(); return window.frameSample; });
		assert.equal(sample.removals, 0); assert.ok(sample.draws > 0); assert.equal(sample.clears, sample.draws, 'every clear is paired with a paint');
		await page.getByRole('button', { name: 'Hide keyboard', exact: true }).click();
		// Chat updates must not reset page loading, even transiently. Watch the
		// actual toolbar attribute so a flip back before the assertion cannot hide.
		for (const loading of [true, false]) {
			await page.evaluate(loading => window.browserFixture.page(loading), loading);
			const label = loading ? 'Stop loading' : 'Reload page';
			await page.getByRole('button', { name: label, exact: true }).waitFor();
			await page.evaluate(label => {
				window.loadingChanges = 0;
				window.loadingObserver = new MutationObserver(records => { window.loadingChanges += records.length; });
				window.loadingObserver.observe(document.querySelector(`button[aria-label="${label}"]`), {
					attributes: true, attributeFilter: ['aria-label'],
				});
			}, label);
			for (let i = 0; i < 4; i++) {
				await page.evaluate(i => window.browserFixture.setDraft(`Typing ${i}`), i);
				await page.waitForTimeout(20);
				await page.evaluate(i => window.browserFixture.setScrollTop(i * 100), i);
				await page.waitForTimeout(20);
				await page.evaluate(i => window.browserFixture.churn(i), i);
				await page.waitForTimeout(20);
			}
			assert.equal(await page.getByRole('button', { name: label, exact: true }).count(), 1);
			assert.equal(await page.evaluate(() => { window.loadingObserver.disconnect(); return window.loadingChanges; }), 0, 'chat activity must not swap reload/stop');
			assert.equal(await page.evaluate(() => window.browserFixture.socketCount), 1, 'chat activity must not reconnect the browser');
			assert.equal(await page.evaluate(() => document.querySelector('canvas') === window.sampledCanvas), true);
		}
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
			path: join(tmpdir(), `browser-workspace-${viewport.width}.png`),
		});
		await page
			.getByRole('button', { name: 'Return control', exact: true })
			.click();
		assert.equal(
			await page
				.getByRole('button', { name: 'New tab', exact: true })
				.isDisabled(),
			false,
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
		// Expansion covers the chat area, not global navigation; restore keeps width.
		const docked = await page.locator('.browser-panel').boundingBox();
		await page.getByRole('button', { name: 'Expand browser panel' }).click();
		assert.deepEqual(await page.locator('.browser-panel').boundingBox(), await page.locator('.chat-row').boundingBox());
		assert.equal(await page.locator('.chat').evaluate(el => el.inert), true);
		await page.screenshot({ path: join(tmpdir(), `browser-workspace-expanded-${viewport.width}.png`) });
		await page.getByRole('button', { name: 'Restore browser panel' }).click();
		assert.deepEqual(await page.locator('.browser-panel').boundingBox(), docked);
		assert.equal(await page.locator('.chat').evaluate(el => el.inert), false);
		await page.evaluate(() => window.browserFixture.setSession('another-fixture'));
		await page.waitForTimeout(150);
		assert.equal(await page.locator('canvas').count(), 0, 'closed session must not expose previous frame');
		assert.equal(await page.evaluate(() => window.browserFixture.openSockets), 0);
		await page.evaluate(() => window.browserFixture.setSession('isolated-fixture'));
		await page.getByRole('button', { name: 'Close browser panel' }).click();
		await page.evaluate(() => { window.browserFixture.autoOpenSession('another-fixture'); window.browserFixture.setSession('another-fixture'); });
		await page.getByRole('button', { name: 'Close browser panel' }).waitFor();
		await page.evaluate(() => window.browserFixture.setSession('isolated-fixture'));
		assert.equal(await page.locator('.browser-panel').count(), 0, 'A remains dismissed after B browser activity');

		assert.deepEqual(errors, []);
		console.log(
			JSON.stringify({
				viewport,
				overflow: false,
				firstTab: true,
				fixedResize: 'no commands',
				fitChurn: { before: beforeChurn, after: afterChurn, draftAndFocusPreserved: true, sockets: 1 },
				fitResize: 'one command per changed size',
				sessionSwitch: 'visibility and dismissal isolated; no frame in closed session',
				canvasSample: sample,
				stickyKeyboard: true,
				chatActivity: 'typing, scrolling and streaming preserve connection, canvas and reload/stop',
				expandedBounds: 'chat only; restore preserves width',
				presets: true,
				orientation: true,
				release: true,
				passiveResize: 'no commands',
				pageErrors: errors,
				screenshot: join(tmpdir(), `browser-workspace-${viewport.width}.png`),
			}),
		);
		await page.close();

	}
	for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) { await verifyProviderLogin(browser, url, viewport); await verifyColdStart(browser, url, viewport); }
} finally {
	await browser.close();
}
