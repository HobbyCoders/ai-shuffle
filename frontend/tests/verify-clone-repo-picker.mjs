// Real-browser check of the Set up Git dialog's clone section: the GitHub
// repository picker lists, searches, pages and fills the URL field, fits the
// dialog on desktop and phone, and shows the not-connected and error states.
//
// Run from frontend/ while the fixture is serving:
//   npx vite dev --config tests/vite.clone-repo-picker.config.js --host 127.0.0.1 --port 5302 --strictPort
//   node tests/verify-clone-repo-picker.mjs
// Needs browser-mcp's playwright-core (npm ci --ignore-scripts --prefix ../browser-mcp)
// and a Chrome/Chromium binary (CHROMIUM_PATH). CLONE_PICKER_FIXTURE_URL overrides
// the URL; SHOT_DIR saves a screenshot of each case there.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import path from 'node:path';
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
const url =
	process.env.CLONE_PICKER_FIXTURE_URL || 'http://127.0.0.1:5302/tests/clone-repo-picker.html';
const shotDir = process.env.SHOT_DIR;

async function open(page, state) {
	await page.goto(`${url}?state=${state}`);
	await page.waitForFunction(() => !!window.cloneRepoPickerFixture);
	const dialog = page.getByRole('dialog', { name: 'Set up Git' });
	await dialog.locator('button[aria-pressed]', { hasText: 'Clone' }).click();
	return dialog;
}

async function shot(page, name) {
	if (!shotDir) return;
	// Let the mode switch's transitions finish so the shot shows the settled dialog.
	await page.waitForFunction(() => document.getAnimations().every((a) => a.playState !== 'running'));
	await page.screenshot({ path: path.join(shotDir, `${name}.png`) });
}

// Nothing in the dialog may stick out sideways (a phone must not scroll horizontally).
async function assertNoOverflow(page) {
	const wide = await page.evaluate(() => {
		const out = [];
		for (const el of document.querySelectorAll('[role="dialog"] *')) {
			const r = el.getBoundingClientRect();
			if (r.width && (r.right > innerWidth + 0.5 || r.left < -0.5))
				out.push(`${el.tagName.toLowerCase()}.${[...el.classList].join('.')} ${Math.round(r.left)}..${Math.round(r.right)}`);
		}
		return out;
	});
	assert.deepEqual(wide.slice(0, 5), [], `${wide.length} elements overflow the viewport`);
}

let failures = 0;
for (const [width, height] of [
	[1280, 800],
	[390, 844]
]) {
	const size = `${width}x${height}`;
	const cases = {
		async connected(page) {
			const dialog = await open(page, 'connected');
			const first = dialog.getByRole('button', { name: /^hobbycoders\/ai-shuffle, private, updated/ });
			await first.waitFor();
			assert.equal(await dialog.getByText('30 of 45').count(), 1);
			await assertNoOverflow(page);
			await shot(page, `${size}-connected`);

			await first.click();
			const urlField = dialog.getByLabel('Repository URL');
			assert.equal(await urlField.inputValue(), 'https://github.com/hobbycoders/ai-shuffle.git');
			assert.equal(await first.getAttribute('aria-pressed'), 'true');
			// The dialog's own Clone button stays reachable (scrolls into view inside the dialog).
			const submit = dialog.locator('button:not([aria-pressed])', { hasText: /^\s*Clone\s*$/ });
			await submit.scrollIntoViewIfNeeded();
			assert.ok(await submit.isVisible() && await submit.isEnabled(), 'Clone button not usable');
			await shot(page, `${size}-selected`);

			await dialog.getByRole('button', { name: 'Load more' }).click();
			await dialog.getByText('45 of 45').waitFor();
			assert.equal(await dialog.getByRole('button', { name: 'Load more' }).count(), 0);

			await dialog.getByLabel('Search repositories').fill('acme project-1');
			await dialog.getByText(/^\d+ of \d+$/).filter({ hasText: /^[1-9] of [1-9]$/ }).waitFor();
			const names = await dialog.locator('ul.repo-list button').evaluateAll((b) => b.map((x) => x.getAttribute('aria-label')));
			assert.ok(names.length > 0 && names.every((n) => n.startsWith('acme-org/project-1')), names.join('; '));
			// Enter in the search box must not start a clone.
			await page.keyboard.press('Enter');
			assert.equal(await dialog.count(), 1);
			await assertNoOverflow(page);
			await shot(page, `${size}-search`);
		},
		async disconnected(page) {
			const dialog = await open(page, 'disconnected');
			await dialog.getByRole('button', { name: /Connect GitHub/ }).waitFor();
			assert.equal(await dialog.getByLabel('Repository URL').count(), 1);
			await assertNoOverflow(page);
			await shot(page, `${size}-disconnected`);
		},
		async error(page) {
			const dialog = await open(page, 'error');
			await dialog.getByText(/GitHub could not list your repositories/).waitFor();
			await dialog.getByRole('button', { name: 'Retry' }).waitFor();
			await assertNoOverflow(page);
			await shot(page, `${size}-error`);
		}
	};
	for (const [name, run] of Object.entries(cases)) {
		const label = `${size} ${name}`;
		const page = await browser.newPage({ viewport: { width, height } });
		page.on('pageerror', (error) => console.error(`[${label}] pageerror: ${error.message}`));
		try {
			await run(page);
			console.log(`PASS ${label}`);
		} catch (error) {
			failures++;
			console.error(`FAIL ${label}: ${error.message}`);
		} finally {
			await page.close();
		}
	}
}
await browser.close();
if (failures) process.exit(1);
