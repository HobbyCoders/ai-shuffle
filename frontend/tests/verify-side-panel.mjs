// Real-browser check of the redesigned side panel: the rail, More, Customize,
// the filter sheet, row gestures with the Undo banner, the Assistant row in
// both states, the update check (#1623), and the phone sheet drag-to-dismiss.
//
// Run from frontend/ while the fixture is serving:
//   npx vite dev --config tests/vite.side-panel.config.js --host 127.0.0.1 --port 5264 --strictPort
//   node tests/verify-side-panel.mjs
// Needs browser-mcp's playwright-core (npm ci --ignore-scripts --prefix ../browser-mcp)
// and a Chrome/Chromium binary (CHROMIUM_PATH). SIDE_PANEL_FIXTURE_URL overrides
// the URL; SHOT_DIR saves screenshots.
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
const base = process.env.SIDE_PANEL_FIXTURE_URL || 'http://127.0.0.1:5264/tests/side-panel.html';
async function shot(page, name) {
	if (!process.env.SHOT_DIR) return;
	await page.waitForTimeout(450); // let sheets finish their entrance
	await page.screenshot({ path: `${process.env.SHOT_DIR}/side-panel-${name}.png` });
}

async function open(viewport, touch, query) {
	const context = await browser.newContext({ viewport, isMobile: touch, hasTouch: touch, deviceScaleFactor: 1, serviceWorkers: 'block' });
	const page = await context.newPage();
	const errors = [];
	page.on('pageerror', (e) => errors.push(e.message));
	await page.goto(`${base}?${query}`);
	await page.getByRole('navigation', { name: 'Workspace' }).waitFor();
	await page.locator('.scard').first().waitFor();
	const cdp = touch ? await context.newCDPSession(page) : null;
	return { context, page, errors, cdp };
}

const log = (page) => page.getByTestId('log').locator('li').allTextContents();
const rowTitles = (page) => page.locator('.scard .title, .scard [data-title]').allTextContents();
const sectionOf = (page, title) =>
	page.locator('.sect', { has: page.locator('.scard', { hasText: title }) }).first().getAttribute('data-section');

async function swipe(page, cdp, title, dx) {
	const card = page.locator('.scard', { hasText: title }).first();
	const box = await card.boundingBox();
	const y = box.y + box.height / 2;
	const x0 = dx > 0 ? box.x + 20 : box.x + box.width - 20;
	const point = (x) => [{ x, y, id: 1 }];
	await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: point(x0) });
	for (let step = 1; step <= 12; step++) {
		await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: point(x0 + (step * dx) / 12) });
		await page.waitForTimeout(16);
	}
	await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
	await page.waitForTimeout(800);
}

async function longPress(page, cdp, locator, ms = 700) {
	const box = await locator.boundingBox();
	const p = [{ x: box.x + box.width / 2, y: box.y + box.height / 2, id: 1 }];
	await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: p });
	await page.waitForTimeout(ms);
	await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
	await page.waitForTimeout(150);
}

async function desktop() {
	const name = 'desktop';
	const { context, page, errors } = await open({ width: 1440, height: 900 }, false, 'assistant=setup');
	await shot(page, `${name}-initial`);

	// Assistant not set up: the row opens its settings.
	await page.getByRole('button', { name: 'Open your assistant' }).click();
	assert.deepEqual(await log(page), ['open feature assistant'], `[${name}] assistant row opens setup`);

	// Rail destination opens its panel.
	await page.getByRole('navigation', { name: 'Workspace' }).getByRole('button', { name: 'Files' }).click();
	assert.equal((await log(page)).at(-1), 'open feature file-browser', `[${name}] rail opens Files`);

	// More menu, then Check for updates (#1623).
	await page.getByRole('button', { name: 'More' }).click();
	const menu = page.getByRole('menu', { name: 'More destinations' });
	await menu.waitFor();
	await shot(page, `${name}-more`);
	await menu.getByRole('menuitem', { name: /Check for updates/ }).click();
	await page.waitForFunction(() => window.fixture.updateRequests.length === 1);
	assert.deepEqual(await page.evaluate(() => window.fixture.updateRequests), [{ action: 'check' }], `[${name}] update check posts`);

	// Customize the rail from More.
	await page.getByRole('button', { name: 'More' }).click();
	await page.getByRole('menuitem', { name: /Customize/ }).click();
	const customize = page.getByRole('dialog', { name: 'Customize the rail' });
	await customize.waitFor();
	await shot(page, `${name}-customize`);
	await page.keyboard.press('Escape');
	await customize.waitFor({ state: 'detached' });

	// Filter sheet narrows the ledger to one project.
	await page.getByRole('button', { name: 'Filter', exact: true }).click();
	const filter = page.getByRole('dialog', { name: 'Filter sessions' });
	await filter.waitFor();
	await shot(page, `${name}-filter`);
	await filter.getByRole('button', { name: 'notes', exact: true }).click();
	await page.keyboard.press('Escape');
	await page.waitForTimeout(200);
	await shot(page, `${name}-filtered`);
	const filtered = await page.locator('.scard').allTextContents();
	assert.ok(filtered.every((t) => /release notes|Play Console/.test(t)), `[${name}] filter keeps only notes rows: ${filtered.join(' | ')}`);
	await page.getByRole('button', { name: 'Clear project filter notes' }).click();
	await page.waitForTimeout(150);

	// Hover action settles a row and the Undo banner brings it back.
	const card = page.getByRole('group', { name: 'Fix the update check on desktop session' });
	await card.hover();
	await card.getByRole('button', { name: 'Settle session' }).click();
	await page.waitForTimeout(800);
	assert.deepEqual(
		await page.evaluate(() => window.fixture.patches.at(-1)),
		{ id: 'a2', body: { is_active: false } },
		`[${name}] hover settle patches the row`,
	);
	await shot(page, `${name}-settled`);
	assert.deepEqual(errors, [], `[${name}] no page errors`);
	await context.close();
	console.log(`ok ${name}`);
}

async function phone() {
	const name = 'phone';
	const { context, page, errors, cdp } = await open({ width: 390, height: 844 }, true, 'ui=mobile&assistant=ready');
	await shot(page, `${name}-initial`);

	// Assistant set up: the row opens the one chat, which never shows in the ledger.
	assert.ok(!(await page.locator('.scard', { hasText: 'Assistant home' }).count()), `[${name}] home chat stays out of the ledger`);
	await page.getByRole('button', { name: 'Open your assistant' }).tap();
	assert.deepEqual(await log(page), ['open session home'], `[${name}] assistant row opens its chat`);

	// Swipe right settles; swipe left archives.
	await swipe(page, cdp, 'Fix the update check', 240);
	assert.deepEqual(await page.evaluate(() => window.fixture.patches.at(-1)), { id: 'a2', body: { is_active: false } }, `[${name}] swipe right settles`);
	await swipe(page, cdp, 'Rename the build tab', -240);
	const archived = await page.evaluate(() => window.fixture.patches.at(-1));
	assert.equal(archived.id, 's3', `[${name}] swipe left archives the row`);
	assert.equal(archived.body.archived, true, `[${name}] swipe left archives the row`);
	assert.equal(await page.locator('.scard', { hasText: 'Rename the build tab' }).count(), 0, `[${name}] archived row leaves`);
	await shot(page, `${name}-after-swipes`);

	// Long press starts selection; Settle the picks, then Undo.
	await longPress(page, cdp, page.locator('.scard', { hasText: 'Redesign the mobile side panel' }).first());
	await page.locator('.scard', { hasText: 'Draft the release notes' }).first().tap();
	await shot(page, `${name}-selecting`);
	const before = page.evaluate(() => window.fixture.patches.length);
	await page.locator('.selbar').getByRole('button', { name: 'Settle' }).tap();
	await page.waitForTimeout(400);
	assert.equal((await page.evaluate(() => window.fixture.patches.length)) - (await before), 2, `[${name}] bulk settle patches both`);
	const undo = page.getByRole('button', { name: 'Undo' });
	await undo.waitFor();
	await shot(page, `${name}-undo-banner`);
	await undo.tap();
	await page.waitForTimeout(400);
	const lastTwo = await page.evaluate(() => window.fixture.patches.slice(-2).map((p) => p.body.is_active));
	assert.deepEqual(lastTwo, [true, true], `[${name}] Undo restores both rows`);
	assert.equal(await page.evaluate(() => document.querySelector('.col').scrollTop), 0, `[${name}] the selection bar leaves the column unscrolled`);

	// Long-press a rail icon opens Customize; Back closes it.
	await longPress(page, cdp, page.getByRole('navigation', { name: 'Workspace' }).getByRole('button', { name: 'Files' }));
	const customize = page.getByRole('dialog', { name: 'Customize the rail' });
	await customize.waitFor();
	await shot(page, `${name}-customize`);
	assert.equal((await log(page)).at(-1), 'open session home', `[${name}] the long press does not also open Files`);
	await customize.getByRole('button', { name: 'Back' }).tap();
	await customize.waitFor({ state: 'detached' });

	// More on the phone is a bottom sheet with the update check; drag it down to dismiss.
	await page.getByRole('button', { name: 'More' }).tap();
	const sheet = page.getByRole('menu', { name: 'More destinations' });
	await sheet.waitFor();
	await page.waitForTimeout(400);
	await shot(page, `${name}-more`);
	assert.equal(await sheet.getByRole('menuitem', { name: 'Check for updates' }).count(), 1, `[${name}] More offers the update check`);
	const box = await sheet.boundingBox();
	const x = box.x + box.width / 2;
	await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y: box.y + 10, id: 1 }] });
	for (let step = 1; step <= 10; step++) {
		await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x, y: box.y + 10 + step * 20, id: 1 }] });
		await page.waitForTimeout(16);
	}
	await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
	await page.waitForTimeout(500);
	assert.equal(await sheet.count(), 0, `[${name}] dragging the sheet down dismisses it`);

	assert.deepEqual(errors, [], `[${name}] no page errors`);
	await context.close();
	console.log(`ok ${name}`);
}

try {
	await desktop();
	await phone();
} finally {
	await browser.close();
}
