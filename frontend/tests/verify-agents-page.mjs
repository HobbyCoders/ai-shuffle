// Browser check of the Agents page (#1825) against tests/agents-page.ts fakes.
// Start the fixture first:
//   npx vite --config tests/vite.agents-page.config.js --port 5291
// then: node tests/verify-agents-page.mjs   (SCREENSHOT_DIR=... to keep shots)
import assert from 'node:assert/strict';
import { join } from 'node:path';
import { mkdirSync } from 'node:fs';
import { createRequire } from 'node:module';
const require = createRequire(new URL('../../browser-mcp/package.json', import.meta.url));
const { chromium } = require('playwright-core');

const base = process.env.AGENTS_FIXTURE_URL || 'http://127.0.0.1:5291/tests/agents-page.html';
const shots = process.env.SCREENSHOT_DIR;
if (shots) mkdirSync(shots, { recursive: true });
const browser = await chromium.launch({
	executablePath:
		process.env.CHROMIUM_PATH ||
		{ win32: 'C:/Program Files/Google/Chrome/Application/chrome.exe' }[process.platform] ||
		'/usr/bin/chromium',
	headless: true,
	args: ['--no-sandbox', '--disable-gpu']
});

const errors = [];
async function open(query, viewport) {
	const page = await browser.newPage({ viewport, deviceScaleFactor: 1 });
	page.on('pageerror', (e) => errors.push(`${query}: ${e.message}`));
	page.on('console', (m) => m.type() === 'error' && !/404|Not Found/.test(m.text()) && errors.push(`${query}: ${m.text()}`));
	await page.goto(`${base}?${query}`);
	await page.waitForSelector('.st-coll');
	await page.waitForTimeout(900);
	return page;
}
const shot = (page, name) => shots && page.screenshot({ path: join(shots, `${name}.png`) });

for (const themeName of ['default', 'vercel']) {
	// Desktop, 1440x900
	let page = await open(`theme=${themeName}`, { width: 1440, height: 900 });
	await page.getByRole('heading', { level: 2, name: 'Juniper' }).waitFor();
	await shot(page, `${themeName}-1440-profile`);
	await page.getByRole('tab', { name: /^Models/ }).click();
	await page.waitForTimeout(300);
	await shot(page, `${themeName}-1440-models`);
	await page.getByRole('button', { name: /^Release Captain/ }).click();
	await page.getByRole('tab', { name: /^Tools/ }).click();
	await page.waitForTimeout(300);
	await shot(page, `${themeName}-1440-tools`);
	await page.getByRole('tab', { name: /^Subagents/ }).click();
	await page.waitForTimeout(300);
	await shot(page, `${themeName}-1440-subagents`);
	await page.getByRole('tab', { name: /^Profile/ }).click();
	await page.getByRole('button', { name: 'Change', exact: true }).click();
	await page.waitForTimeout(300);
	await shot(page, `${themeName}-1440-avatar`);
	await page.getByRole('button', { name: 'New agent', exact: true }).first().click();
	await page.waitForTimeout(300);
	await shot(page, `${themeName}-1440-new`);
	await page.keyboard.press('Escape');
	await page.close();

	// Narrow desktop: 820 wide
	page = await open(`theme=${themeName}`, { width: 820, height: 800 });
	await page.getByRole('heading', { level: 2, name: 'Juniper' }).waitFor();
	await shot(page, `${themeName}-820`);
	// The tab strip overflows: arrow keys keep the active tab in view.
	const strip = page.getByRole('tablist');
	const tabs = await strip.evaluate((el) => ({ sw: el.scrollWidth, cw: el.clientWidth }));
	await page.getByRole('tab', { name: /^Profile/ }).focus();
	await page.keyboard.press('End');
	await page.waitForTimeout(250);
	const advanced = await page.getByRole('tab', { name: /^Advanced/ }).evaluate((el) => {
		const s = el.parentElement.getBoundingClientRect();
		const r = el.getBoundingClientRect();
		return { inView: r.left >= s.left - 1 && r.right <= s.right + 1, selected: el.getAttribute('aria-selected') };
	});
	assert.equal(advanced.selected, 'true');
	assert.ok(advanced.inView, `Advanced tab in view (strip ${tabs.sw}/${tabs.cw})`);
	await shot(page, `${themeName}-820-advanced`);
	await page.close();

	// Phone shell, 390x844: list, then detail, then back
	page = await open(`theme=${themeName}&mobile=1`, { width: 390, height: 844 });
	await shot(page, `${themeName}-390-list`);
	assert.equal(await page.locator('.st-dpane').isVisible(), false, 'phone starts on the list');
	await page.getByRole('button', { name: /^Release Captain/ }).click();
	await page.getByRole('heading', { level: 2, name: 'Release Captain' }).waitFor();
	await page.waitForTimeout(300);
	await shot(page, `${themeName}-390-detail`);
	const tabsBox = await page.getByRole('tablist').evaluate((el) => ({ sw: el.scrollWidth, cw: el.clientWidth }));
	assert.ok(tabsBox.sw > tabsBox.cw, 'tab strip scrolls sideways on the phone');
	await page.getByRole('tab', { name: /^Models/ }).click();
	await page.waitForTimeout(300);
	await shot(page, `${themeName}-390-models`);
	await page.getByRole('button', { name: 'Back to Agents', exact: true }).click();
	await page.waitForTimeout(300);
	assert.equal(await page.locator('.st-lcol').isVisible(), true, 'back returns to the list');
	await page.getByRole('button', { name: 'New agent', exact: true }).first().click();
	await page.waitForTimeout(300);
	await shot(page, `${themeName}-390-new`);
	await page.close();
}

// States: 404 default, empty, loading
let page = await open('default=404&colors=0', { width: 1440, height: 900 });
await page.getByRole('tab', { name: /^Models/ }).click();
await page.waitForTimeout(300);
assert.equal(await page.getByText('Subagent default').count(), 0);
await shot(page, 'state-404-models');
await page.close();
page = await open('empty=1', { width: 1440, height: 900 });
await page.waitForTimeout(300);
await shot(page, 'state-empty');
await page.close();
page = await open('loading=1', { width: 1440, height: 900 });
await shot(page, 'state-loading');
await page.close();

await browser.close();
assert.deepEqual(errors, [], errors.join('\n'));
console.log('agents page verified');
