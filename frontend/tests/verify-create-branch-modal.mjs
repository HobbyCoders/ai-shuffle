// Real-browser regression: the composer's "New branch..." dialog must sit above
// the composer. It is mounted from the chip row, which paints below the editor
// row, so without a portal the editor covered the dialog's branch-name input.
//
// Run from frontend/ while the fixture is serving:
//   npx vite dev --config tests/vite.create-branch-modal.config.js --host 127.0.0.1 --port 5301 --strictPort
//   node tests/verify-create-branch-modal.mjs
// Needs browser-mcp's playwright-core (npm ci --ignore-scripts --prefix ../browser-mcp)
// and a Chrome/Chromium binary (CHROMIUM_PATH). CREATE_BRANCH_FIXTURE_URL overrides the URL.
import assert from 'node:assert/strict';
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
const url =
	process.env.CREATE_BRANCH_FIXTURE_URL || 'http://127.0.0.1:5301/tests/create-branch-modal.html';

// Each composer path that opens the dialog: the worktree base-branch chip
// (ExecutionModeBar) and, in local mode, the checkout branch chip (BranchSelector).
const PATHS = [
	{ mode: 'worktree', chip: /^Worktree branch:/ },
	{ mode: 'local', chip: /^(Branch:|Select branch)/ }
];
let failures = 0;
try {
	for (const [width, height] of [
		[1440, 900],
		[390, 844]
	]) {
		for (const { mode, chip } of PATHS) {
			const label = `${width}x${height} ${mode}`;
			const page = await browser.newPage({ viewport: { width, height } });
			page.on('pageerror', (error) => console.error(`[${label}] pageerror: ${error.message}`));
			try {
				await page.goto(`${url}?mode=${mode}`);
				await page.waitForFunction(() => !!window.createBranchFixture);
				await page.getByRole('button', { name: chip }).click();
				// A plain button in the worktree dropdown, a menuitem in BranchSelector's.
				await page.locator('button', { hasText: 'New branch...' }).click();
				const dialog = page.getByRole('dialog', { name: 'Create a branch' });
				await dialog.waitFor();
				const input = dialog.getByLabel('Branch name');
				const hit = await input.evaluate((el) => {
					const r = el.getBoundingClientRect();
					const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
					return {
						isInput: top === el,
						top: top ? `${top.tagName.toLowerCase()}.${[...top.classList].join('.')}` : null
					};
				});
				assert.ok(hit.isInput, `elementFromPoint at the input is ${hit.top}`);
				// The backdrop covers the viewport, so nothing in the composer
				// (chips, editor row, controls row) may paint above it anywhere.
				const leaks = await dialog.evaluate((el) => {
					const out = [];
					for (let y = 4; y < innerHeight; y += 24)
						for (let x = 4; x < innerWidth; x += 24) {
							const top = document.elementFromPoint(x, y);
							if (top && !el.contains(top))
								out.push(`${x},${y} ${top.tagName.toLowerCase()}.${[...top.classList].join('.')}`);
						}
					return out;
				});
				assert.deepEqual(leaks.slice(0, 5), [], `${leaks.length} points paint above the dialog`);
				// A real click and typing reach the input.
				await input.click();
				await input.fill('');
				await page.keyboard.type('fix-stacking');
				assert.equal(await input.inputValue(), 'fix-stacking');
				// Tab stays inside the dialog.
				for (let i = 0; i < 6; i++) {
					await page.keyboard.press('Tab');
					assert.ok(
						await dialog.evaluate((el) => el.contains(document.activeElement)),
						'focus left the dialog on Tab'
					);
				}
				await page.keyboard.press('Escape');
				await dialog.waitFor({ state: 'detached' });
				console.log(`PASS ${label}`);
			} catch (error) {
				failures++;
				console.error(`FAIL ${label}: ${error.message}`);
			} finally {
				await page.close();
			}
		}
	}
} finally {
	await browser.close();
}
if (failures) process.exit(1);
