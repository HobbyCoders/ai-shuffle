// Real-browser check for the per-message copy action in the real ChatView, at
// desktop width (hover / keyboard reveal) and as the phone app (always shown).
// Asserts: assistant blocks copy their Markdown source, user prompts keep the
// plain-text copy, revealing the row never moves the transcript, keyboard
// focus reveals it, a still-streaming reply offers no copy, and on the phone
// shell the 32px button carries a 44px touch target.
//
// Run from frontend/ while the fixture is serving:
//   npx vite dev --config tests/vite.message-copy.config.js --host 127.0.0.1 --port 5291 --strictPort
//   node tests/verify-message-copy.mjs
// Needs browser-mcp's playwright-core (npm ci --ignore-scripts --prefix ../browser-mcp)
// and Chrome/Chromium (CHROMIUM_PATH). MESSAGE_COPY_FIXTURE_URL overrides the
// URL; SCREENSHOT_DIR keeps screenshots.
import assert from 'node:assert/strict';
import { mkdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createRequire } from 'node:module';
const require = createRequire(new URL('../../browser-mcp/package.json', import.meta.url));
const { chromium } = require('playwright-core');

const browser = await chromium.launch({
	executablePath:
		process.env.CHROMIUM_PATH ||
		{
			win32: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
			darwin: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
		}[process.platform] ||
		'/usr/bin/chromium',
	headless: true,
	args: ['--no-sandbox', '--disable-gpu']
});
const base = process.env.MESSAGE_COPY_FIXTURE_URL || 'http://127.0.0.1:5291/tests/message-copy.html';
const shots = process.env.SCREENSHOT_DIR || join(tmpdir(), 'message-copy-shots');
await mkdir(shots, { recursive: true });

async function open(query, options) {
	const context = await browser.newContext(options);
	await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: new URL(base).origin });
	const page = await context.newPage();
	const errors = [];
	page.on('pageerror', (error) => errors.push(String(error)));
	await page.goto(`${base}${query}`);
	await page.waitForFunction(() => window.messageCopyFixtureReady === true);
	await page.getByText('Pin', { exact: false }).first().waitFor();
	await page.waitForTimeout(300);
	return { context, page, errors };
}

const finalBlock = (page) => page.locator('[role="article"]').filter({ hasText: 'Pin vite to' }).first();
const opacity = (locator) => locator.evaluate((el) => Number(getComputedStyle(el).opacity));
// The Windows system clipboard hands line breaks back as CRLF.
const clipboard = (page) => page.evaluate(() => navigator.clipboard.readText()).then((t) => t.replace(/\r\n/g, '\n'));

// ── Desktop: hover and keyboard reveal, Markdown source copy ────────────────
{
	const { context, page, errors } = await open('', { viewport: { width: 1280, height: 900 } });
	const FINAL = await page.evaluate(() => window.FINAL_ANSWER);
	const block = finalBlock(page);
	const row = block.locator('.assistant-actions');
	const button = block.getByRole('button', { name: 'Copy message' });
	await block.scrollIntoViewIfNeeded();
	await page.mouse.move(5, 5);
	await page.waitForTimeout(250);
	assert.equal(await opacity(row), 0, 'desktop actions start hidden');
	const before = await block.boundingBox();
	await page.screenshot({ path: join(shots, 'desktop-rest.png') });

	await block.hover();
	await page.waitForTimeout(250);
	assert.equal(await opacity(row), 1, 'hover reveals the assistant actions');
	const after = await block.boundingBox();
	assert.deepEqual(after, before, 'revealing the actions does not move or resize the message');
	await page.screenshot({ path: join(shots, 'desktop-hover.png') });

	await button.click();
	await page.getByRole('button', { name: 'Copied' }).waitFor();
	assert.equal(await clipboard(page), FINAL, 'assistant copy is the Markdown source');
	await page.screenshot({ path: join(shots, 'desktop-copied.png') });

	// Keyboard: Tab from the top of the page until the copy button holds focus.
	await page.mouse.move(5, 5);
	await page.evaluate(() => (document.activeElement instanceof HTMLElement) && document.activeElement.blur());
	await page.waitForTimeout(2200);
	let reached = false;
	for (let i = 0; i < 80 && !reached; i++) {
		await page.keyboard.press('Tab');
		reached = await button.evaluate((el) => el === document.activeElement);
	}
	assert.ok(reached, 'the assistant copy button is reachable by Tab');
	await page.waitForTimeout(250);
	assert.equal(await opacity(row), 1, 'keyboard focus reveals the actions');
	const ring = await button.evaluate((el) => getComputedStyle(el).outlineStyle);
	assert.equal(ring, 'solid', 'keyboard focus draws a visible ring');
	await page.screenshot({ path: join(shots, 'desktop-keyboard.png') });

	// The user prompt keeps its plain-text copy.
	const prompt = page.locator('.userrow').filter({ hasText: 'Why does the build fail?' });
	await prompt.hover();
	await prompt.getByRole('button', { name: 'Copy message' }).click();
	await page.waitForTimeout(150);
	assert.equal(await clipboard(page), 'Why does the build fail?');

	// Mid-turn commentary folded into the settled work row has its own copy.
	const fold = page.getByRole('button', { name: /Worked|Read 1|Read/ }).first();
	if (await fold.count()) {
		await fold.click();
		const commentary = page.locator('[role="article"]').filter({ hasText: 'Checking the' }).first();
		await commentary.waitFor();
		await commentary.hover();
		await commentary.getByRole('button', { name: 'Copy message' }).click();
		await page.waitForTimeout(150);
		assert.equal(await clipboard(page), await page.evaluate(() => window.COMMENTARY));
		await page.screenshot({ path: join(shots, 'desktop-commentary.png') });
	}
	assert.deepEqual(errors, []);
	await context.close();
}

// ── Desktop: a still-streaming reply offers no copy ─────────────────────────
{
	const { context, page } = await open('?streaming=1', { viewport: { width: 1280, height: 900 } });
	const live = page.locator('[role="article"]').filter({ hasText: 'Running the suite' }).first();
	await live.waitFor();
	await live.hover();
	assert.equal(await live.getByRole('button', { name: 'Copy message' }).count(), 0, 'no copy while streaming');
	await context.close();
}

// ── Touch layouts: always visible, 44px touch target ────────────────────────
// The phone shell at phone and tablet widths (mobile.css), and a narrow
// desktop web window (the components' own media query).
for (const [width, query, label] of [
	[390, '?platform=mobile', 'mobile-390'],
	[360, '?platform=mobile', 'mobile-360'],
	[820, '?platform=mobile', 'mobile-820'],
	[390, '', 'web-narrow-390']
]) {
	const { context, page, errors } = await open(query, {
		viewport: { width, height: 844 },
		deviceScaleFactor: 3,
		isMobile: true,
		hasTouch: true
	});
	const FINAL = await page.evaluate(() => window.FINAL_ANSWER);
	const block = finalBlock(page);
	await block.scrollIntoViewIfNeeded();
	const row = block.locator('.assistant-actions');
	const userRow = page.locator('.userrow .useractions').first();
	assert.equal(await opacity(row), 1, 'assistant actions are visible without hover on the phone');
	assert.equal(await opacity(userRow), 1, 'user actions are visible without hover on the phone');
	const button = block.getByRole('button', { name: 'Copy message' });
	const box = await button.boundingBox();
	assert.ok(box && box.width >= 32 && box.height >= 32, `visual button is 32px (${JSON.stringify(box)})`);
	const hit = await button.evaluate((el) => {
		const r = el.getBoundingClientRect();
		const cx = r.left + r.width / 2;
		const cy = r.top + r.height / 2;
		const probe = (x, y) => el.contains(document.elementFromPoint(x, y));
		return { up: probe(cx, cy - 21), down: probe(cx, cy + 21), left: probe(cx - 21, cy), right: probe(cx + 21, cy) };
	});
	assert.deepEqual(hit, { up: true, down: true, left: true, right: true }, 'touch target spans 44px');
	await page.screenshot({ path: join(shots, `${label}.png`) });
	await button.tap();
	await page.getByRole('button', { name: 'Copied' }).waitFor();
	assert.equal(await clipboard(page), FINAL);
	await page.screenshot({ path: join(shots, `${label}-copied.png`) });
	assert.deepEqual(errors, []);
	await context.close();
}

await browser.close();
console.log(`message copy: all checks passed; screenshots in ${shots}`);
