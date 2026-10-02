// #1636 check: a first message typed into a New Chat view that the page
// restored on load is either sent or kept in the composer with a visible
// reason. Run while the fixture is served:
//   npx vite dev --config tests/vite.composer-send-guard.config.js --host 127.0.0.1 --port 6101 --strictPort
//   CHROMIUM_PATH=/path/to/chromium node tests/verify-composer-send-guard.mjs
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
const baseUrl = process.env.FIXTURE_URL || 'http://127.0.0.1:6101/tests/composer-send-guard.html';
const output = process.env.SCREENSHOT_DIR || '/tmp/composer-send-guard-browser';
await mkdir(output, { recursive: true });
const PROMPT = 'first message after reopening the tab';

async function open(query, width = 1280, height = 800) {
	const page = await browser.newPage({ viewport: { width, height } });
	const errors = [];
	page.on('pageerror', error => errors.push(error.message));
	// The fixture answers every request itself; this only guards against leaks.
	await page.route(url => url.pathname.startsWith('/api/'), route => route.abort());
	await page.goto(baseUrl + query);
	const editor = page.locator('.ProseMirror').first();
	await editor.waitFor();
	const session = () => page.evaluate(() => window.fixtureSession());
	const creates = () => page.evaluate(() => window.fixtureRequests
		.filter(r => r.method === 'POST' && /\/sessions(\/with-worktree)?$/.test(r.path)));
	const queries = () => page.evaluate(() => window.fixtureFrames.filter(f => f.type === 'query'));
	const send = async () => { await editor.click(); await page.keyboard.type(PROMPT); await page.keyboard.press('Enter'); };
	const editorText = () => editor.innerText();
	return { page, editor, errors, session, creates, queries, send, editorText };
}

try {
	for (const [width, height] of [[1280, 800], [390, 844]]) {
		const size = `${width}x${height}`;
		for (const query of ['?hold=projects', '?hold=projects&cluster=1']) {
			// Before the project catalog is in, the restored New Chat's remembered
			// project and branch are not resolved yet (a cluster-linked one has
			// only its shared id). The send is held with the text in place and a
			// loading state, then goes out on the remembered branch once the
			// catalog answers. Nothing may go to the older session the server
			// remembered, or out on an auto-named branch.
			const cluster = query.includes('cluster');
			const { page, errors, session, creates, queries, send, editorText } = await open(query, width, height);
			await page.waitForFunction(() => window.fixtureRequests.some(r => r.path.endsWith('/cluster/projects')));
			assert.equal((await session()).sessionId, null, 'the New Chat view is not bound to the old session');
			assert.equal((await session()).worktreeBranch, '', 'the remembered branch waits for the catalog');
			await send();
			await page.getByRole('button', { name: 'Loading projects…' }).waitFor();
			await page.waitForTimeout(300);
			assert.equal((await editorText()).trim(), PROMPT, 'the held text stays');
			assert.deepEqual(await creates(), [], 'nothing is created while the catalog loads');
			assert.deepEqual(await queries(), [], 'nothing went to the old session');
			await page.screenshot({ path: `${output}/catalog-loading${cluster ? '-cluster' : ''}-${size}.png` });
			await page.evaluate(() => window.fixtureRelease());
			await page.waitForFunction(() => window.fixtureFrames.some(f => f.type === 'query'));
			const created = await creates();
			assert.equal(created.length, 1, 'one create request');
			assert.equal(created[0].body.branch_name, 'live/1528-explicit', 'the remembered branch, not an auto-named one');
			// A cluster-linked pick resolves to this worker's clone of it.
			assert.equal(created[0].body.project_id, 'shuffle-scratch', 'the remembered project, resolved to its local clone');
			const sent = await queries();
			assert.equal(sent.length, 1);
			assert.equal(sent[0].session_id, 'created-session');
			assert.equal(sent[0].prompt, PROMPT);
			await page.waitForTimeout(300);
			assert.equal((await session()).sessionId, 'created-session');
			assert.equal((await editorText()).trim(), '');
			await page.screenshot({ path: `${output}/catalog-sent${cluster ? '-cluster' : ''}-${size}.png` });
			assert.deepEqual(errors, []);
			console.log(`PASS ${size}: a send before the ${cluster ? 'cluster-linked ' : ''}project catalog loads is held, then goes out on the remembered branch`);
			await page.close();
		}
		{
			// The rest of startup still loading (the session list here): the
			// restored New Chat already has its project and branch, and the
			// session a send creates stays open when startup finishes.
			const { page, errors, session, creates, queries, send, editorText } = await open('?hold=sessions', width, height);
			await page.waitForFunction(() => window.fixtureSession().worktreeBranch === 'live/1528-explicit');
			assert.equal((await session()).sessionId, null, 'the New Chat view is not bound to the old session');
			await send();
			await page.waitForFunction(() => window.fixtureFrames.some(f => f.type === 'query'));
			const sent = await queries();
			assert.equal(sent.length, 1);
			assert.equal(sent[0].session_id, 'created-session', 'the message goes to the session it created');
			assert.equal(sent[0].prompt, PROMPT);
			assert.equal((await creates()).length, 1, 'one create request');
			assert.equal((await creates())[0].body.branch_name, 'live/1528-explicit');
			await page.evaluate(() => window.fixtureRelease());
			await page.waitForTimeout(500);
			assert.equal((await session()).sessionId, 'created-session', 'finishing startup keeps the new session open');
			assert.ok((await session()).messages >= 1, 'the sent message is on screen');
			assert.equal((await editorText()).trim(), '');
			await page.screenshot({ path: `${output}/sent-${size}.png` });
			assert.deepEqual(errors, []);
			console.log(`PASS ${size}: a send while startup loads creates the session on the remembered branch, and it stays open`);
			await page.close();
		}
		{
			const { page, errors, session, creates, send, editorText } = await open('?fail=1', width, height);
			await page.waitForFunction(() => window.fixtureSession().worktreeBranch === 'live/1528-explicit');
			await send();
			await page.waitForFunction(() => !!window.fixtureSession().worktreeError);
			assert.equal((await creates()).length, 1);
			assert.equal((await editorText()).trim(), PROMPT, 'a failed create keeps the text');
			assert.match(await page.locator('[role="alert"], [role="status"]').filter({ hasText: 'Server error creating worktree' }).first().innerText(),
				/Server error creating worktree for 'live\/1528-explicit'/);
			await page.screenshot({ path: `${output}/create-failed-${size}.png` });
			assert.deepEqual(errors, []);
			console.log(`PASS ${size}: a failed create keeps the text and shows why`);
			await page.close();
		}
		{
			// The auto-named branch path echoes the message into the transcript
			// while it creates; a failed create takes the echo back and the
			// composer gets the text.
			const { page, errors, session, creates, send, editorText } = await open('?checkout=auto&fail=1', width, height);
			await page.waitForFunction(() => window.fixtureSession().project === 'shuffle-scratch');
			await send();
			await page.waitForFunction(() => !!window.fixtureSession().worktreeError);
			assert.equal((await creates()).length, 1);
			assert.equal((await session()).messages, 0, 'the echo is gone');
			assert.equal((await editorText()).trim(), PROMPT, 'a failed auto-branch create keeps the text');
			assert.ok(await page.getByText(/Server error creating worktree/).first().isVisible(), 'the reason is on screen');
			await page.screenshot({ path: `${output}/auto-create-failed-${size}.png` });
			assert.deepEqual(errors, []);
			console.log(`PASS ${size}: a failed auto-branch create keeps the text and shows why`);
			await page.close();
		}
		{
			const { page, errors, session, creates, queries, send, editorText } = await open('?checkout=existing&ws=down', width, height);
			await page.waitForFunction(() => window.fixtureSession().worktreeId === 'wt-1');
			await send();
			await page.waitForFunction(() => !!window.fixtureSession().error, null, { timeout: 8000 });
			assert.deepEqual(await creates(), []);
			assert.deepEqual(await queries(), []);
			assert.equal((await editorText()).trim(), PROMPT, 'a refused send keeps the text');
			assert.match((await session()).error, /Unable to connect/);
			assert.ok(await page.getByText(/Unable to connect/).first().isVisible(), 'the reason is on screen');
			await page.screenshot({ path: `${output}/not-connected-${size}.png` });
			assert.deepEqual(errors, []);
			console.log(`PASS ${size}: an existing-worktree send with no connection keeps the text and shows why`);
			await page.close();
		}
		{
			// The branch session is created, but its first message cannot go out.
			const { page, errors, session, creates, queries, send, editorText } = await open('?ws=down', width, height);
			await page.waitForFunction(() => window.fixtureSession().worktreeBranch === 'live/1528-explicit');
			await send();
			await page.waitForFunction(() => !!window.fixtureSession().error, null, { timeout: 12000 });
			assert.equal((await creates()).length, 1);
			assert.deepEqual(await queries(), []);
			assert.equal((await session()).sessionId, 'created-session');
			assert.equal((await editorText()).trim(), PROMPT, 'the text comes back on the created session');
			assert.ok(await page.getByText(/Unable to connect/).first().isVisible(), 'the reason is on screen');
			await page.screenshot({ path: `${output}/created-not-sent-${size}.png` });
			assert.deepEqual(errors, []);
			console.log(`PASS ${size}: a created session whose first message cannot be sent gets the text back with the reason`);
			await page.close();
		}
	}
	console.log(`PASS all composer send guard checks; screenshots: ${output}`);
} finally {
	await browser.close();
}
