// #1616 desktop check: the new-session composer shows the call button, and a
// click creates the session and then starts the call on it.
// Run while the fixture is served:
//   npx vite dev --config tests/vite.fresh-call.config.js --port 5932 --strictPort
//   CHROMIUM_PATH=/path/to/chromium node tests/verify-fresh-call.mjs
// Needs browser-mcp's playwright-core (`cd ../browser-mcp && npm ci`).
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { mkdir } from 'node:fs/promises';

const require = createRequire(new URL('../../browser-mcp/package.json', import.meta.url));
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright-core');
const browser = await chromium.launch({
	headless: true,
	executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium',
	args: ['--no-sandbox']
});
const baseUrl = process.env.FIXTURE_URL || 'http://localhost:5932/tests/fresh-call.html';
const output = process.env.SCREENSHOT_DIR || '/tmp/fresh-call-browser';
await mkdir(output, { recursive: true });

async function open(query = '') {
	const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
	const errors = [];
	page.on('pageerror', error => errors.push(error.message));
	// The fixture answers every request itself; this only guards against leaks.
	await page.route(url => url.pathname.startsWith('/api/'), route => route.abort());
	await page.goto(baseUrl + query);
	const call = page.locator('.voice-call-btn');
	await call.waitFor();
	const posts = () => page.evaluate(() => window.fixtureCalls.filter(c => c.method === 'POST'));
	return { page, call, posts, errors };
}

try {
	{
		const { page, call, posts, errors } = await open();
		assert.equal(await call.getAttribute('aria-label'), 'Start a call in a new conversation');
		assert.equal(await call.isDisabled(), false, 'available on a fresh composer');
		await page.screenshot({ path: `${output}/fresh-call-ready.png` });
		await call.dblclick();
		await page.waitForFunction(() => window.fixtureCalls.some(c => c.path.endsWith('/attach')));
		await page.waitForFunction(() => window.fixtureCall().status === 'error');
		const sent = await posts();
		assert.deepEqual(sent.map(c => c.path), ['/api/v1/sessions', '/api/v1/voice/sessions/fixture-call-session/attach'],
			'one create, then one attach on the created session');
		assert.equal(sent[0].body.profile_id, 'fixture-agent');
		assert.equal(sent[0].body.mode, 'code');
		assert.equal((await page.evaluate(() => window.fixtureSession())).sessionId, 'fixture-call-session');
		assert.equal((await page.evaluate(() => window.fixtureCall())).sessionId, 'fixture-call-session');
		await page.screenshot({ path: `${output}/fresh-call-started.png` });
		assert.deepEqual(errors, []);
		console.log('PASS 1280x800: click creates the session, then starts the call on it (mic denied by the fixture)');
		await page.close();
	}
	{
		const { page, call, posts, errors } = await open('?fail=1');
		await call.click();
		await page.waitForFunction(() => !!window.fixtureSession().error);
		await page.waitForTimeout(200);
		assert.deepEqual((await posts()).map(c => c.path), ['/api/v1/sessions'], 'no call after a failed create');
		assert.equal((await page.evaluate(() => window.fixtureCall())).status, 'idle');
		assert.match((await page.evaluate(() => window.fixtureSession())).error, /no longer exists/);
		assert.equal(await call.isDisabled(), false, 'the button can be pressed again');
		assert.deepEqual(errors, []);
		console.log('PASS 1280x800: a failed create shows its error and starts no call');
		await page.close();
	}
	{
		const { page, call, posts, errors } = await open('?device=worker');
		assert.equal(await call.isDisabled(), true);
		assert.match(await call.getAttribute('title'), /Switch the runner to Local.*Studio PC/);
		await call.click({ force: true });
		await page.waitForTimeout(200);
		assert.deepEqual(await posts(), [], 'nothing is created or called');
		await page.screenshot({ path: `${output}/fresh-call-worker.png` });
		assert.deepEqual(errors, []);
		console.log('PASS 1280x800: with another device picked the button is disabled with the reason');
		await page.close();
	}
	console.log(`PASS all fresh-call checks; screenshots: ${output}`);
} finally {
	await browser.close();
}
