// Real-browser check for #1528: a branch choice in the new-session composer
// (New branch... or a branch picked from the list) is one-time. Drives the production ExecutionModeBar,
// CreateBranchModal, activeSession and composer preferences with a local
// transport (one-time-branch.ts) and a stub git store.
//
// Run from frontend/ while the fixture is serving:
//   npx vite dev --config tests/vite.one-time-branch.config.js --host 127.0.0.1 --port 5401 --strictPort
//   node tests/verify-one-time-branch.mjs
// Needs browser-mcp's playwright-core (npm ci --ignore-scripts --prefix ../browser-mcp)
// and a Chrome/Chromium binary (CHROMIUM_PATH). ONE_TIME_BRANCH_FIXTURE_URL overrides the URL.
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
const url = process.env.ONE_TIME_BRANCH_FIXTURE_URL || 'http://127.0.0.1:5401/tests/one-time-branch.html';
let passed = 0;

async function scenario(name, run) {
	const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, serviceWorkers: 'block' });
	const page = await context.newPage();
	const errors = [];
	page.on('pageerror', (e) => errors.push(e.message));
	await page.goto(url);
	const chip = page.getByRole('button', { name: /^Worktree branch:/ });
	await chip.waitFor();
	const fx = (fn) => page.evaluate(fn);
	const check = (condition, label, detail) => {
		assert.ok(condition, `[${name}] ${label}${detail === undefined ? '' : `: ${JSON.stringify(detail)}`}`);
		passed++;
	};
	const chipIs = async (label) => {
		await page.waitForFunction((l) => document.querySelector('.branch-trigger')?.getAttribute('aria-label') === `Worktree branch: ${l}`, label, { timeout: 3000 })
			.catch(() => {});
		const actual = await chip.getAttribute('aria-label');
		check(actual === `Worktree branch: ${label}`, `chip shows ${label}`, actual);
	};
	const button = (label) => page.getByRole('button', { name: label, exact: true });
	const newBranch = async (branch) => {
		await chip.click();
		await page.getByText('New branch...', { exact: true }).click();
		await page.getByLabel('Branch name').fill(branch);
		await button('Continue').click();
		await chipIs(branch);
		check((await fx(() => window.oneTimeBranch.createdBranches)).includes(branch), 'modal created the branch up front');
	};
	await run({ page, fx, check, chipIs, button, newBranch });
	check(errors.length === 0, 'no page errors', errors);
	await context.close();
}

await scenario('repro: New branch, send, New session mid-create', async ({ page, fx, check, chipIs, button, newBranch }) => {
	await chipIs('Auto from main');
	await newBranch('live/1528-explicit');
	check((await fx(() => window.oneTimeBranch.storedCheckout()))?.branch === 'live/1528-explicit', 'choice remembered until a session is created');
	await fx(() => window.oneTimeBranch.holdNextCreate());
	await button('Send').click();
	await page.waitForFunction(() => window.oneTimeBranch.creates.length === 1);
	const create = (await fx(() => window.oneTimeBranch.creates))[0];
	check(create.branch_name === 'live/1528-explicit' && create.create_new_branch === false, 'create request', create);
	check(await fx(() => window.oneTimeBranch.checkoutClaimed), 'choice claimed while in flight');
	// The user starts the next task before the first create returns.
	await button('New session').click();
	await chipIs('Auto from main');
	await fx(() => window.oneTimeBranch.finishCreate('ok'));
	await page.waitForFunction(() => !window.oneTimeBranch.checkoutClaimed);
	check((await fx(() => window.oneTimeBranch.storedCheckout())) === null, 'stored choice consumed after success');
	await chipIs('Auto from main');
	await button('New session').click();
	await chipIs('Auto from main');
	check((await fx(() => window.oneTimeBranch.state())).worktreeBranch === '', 'new compose has no staged branch');
});

await scenario('repro without overlap: send completes, then New session', async ({ page, fx, check, chipIs, button, newBranch }) => {
	await newBranch('live/1528-explicit');
	await button('Send').click();
	await page.getByRole('status').filter({ hasText: 'Created session-1' }).waitFor();
	check((await fx(() => window.oneTimeBranch.storedCheckout())) === null, 'stored choice consumed after success');
	await button('New session').click();
	await chipIs('Auto from main');
});

await scenario('failed create keeps the choice', async ({ page, fx, check, chipIs, button, newBranch }) => {
	await newBranch('live/fails');
	await fx(() => window.oneTimeBranch.holdNextCreate());
	await button('Send').click();
	await page.waitForFunction(() => window.oneTimeBranch.creates.length === 1);
	await fx(() => window.oneTimeBranch.finishCreate('fail'));
	await page.getByRole('status').filter({ hasText: 'Create failed' }).waitFor();
	await chipIs('live/fails');
	const stored = await fx(() => window.oneTimeBranch.storedCheckout());
	check(stored?.branch === 'live/fails', 'stored choice kept', stored);
	check(!(await fx(() => window.oneTimeBranch.checkoutClaimed)), 'claim released');
	await button('New session').click();
	await chipIs('live/fails');
});

await scenario('existing branch picked from the list is one-time too', async ({ page, fx, check, chipIs, button }) => {
	await page.getByRole('button', { name: /^Worktree branch:/ }).click();
	// The list under "Branches" (not the base-branch picker above it).
	await page.locator('.branch-section-label', { hasText: 'Branches' }).waitFor();
	await page.locator('.branch-dropdown .branch-item', { hasText: 'develop' }).last().click();
	await chipIs('develop');
	check((await fx(() => window.oneTimeBranch.storedCheckout()))?.branch === 'develop', 'listed pick remembered before send');
	await fx(() => window.oneTimeBranch.holdNextCreate());
	await button('Send').click();
	await page.waitForFunction(() => window.oneTimeBranch.creates.length === 1);
	const create = (await fx(() => window.oneTimeBranch.creates))[0];
	check(create.branch_name === 'develop' && create.create_new_branch === false, 'create request', create);
	await button('New session').click();
	await chipIs('Auto from main');
	await fx(() => window.oneTimeBranch.finishCreate('ok'));
	await page.waitForFunction(() => !window.oneTimeBranch.checkoutClaimed);
	check((await fx(() => window.oneTimeBranch.storedCheckout())) === null, 'listed pick consumed after success');
	await button('New session').click();
	await chipIs('Auto from main');
});

await browser.close();
console.log(`one-time branch fixture: ${passed} checks passed`);
