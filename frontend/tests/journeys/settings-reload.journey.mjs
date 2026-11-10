// Reloading the app on a Settings page: the page's rows hold their space
// while its data loads, so nothing already painted is pushed down (#1301).
// `--all-pages` adds every other Settings page (measurement, not the suite).
import { runJourney } from './runner.mjs';
import { expect, gotoApp, settleServiceWorker, showSidebar } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const ALL = ['General', 'Voice', 'Providers', 'Default models', 'Model tuning', 'Usage', 'Memory', 'Integrations', 'Plugins', 'MCP servers', 'Hooks', 'Devices', 'Cloud', 'Security', 'System', 'Logs'];
// Security and Usage shifted for #1301 (a loading placeholder smaller than the
// loaded control); the rest are measured with --all-pages.
const pages = process.argv.includes('--all-pages') ? ALL : ['Security', 'Usage'];
const slug = (label) => label.toLowerCase().replace(/[^a-z]+/g, '-');

const reloadCase = (label) => ({
	name: `reload-on-${slug(label)}`,
	viewports: ['desktop', 'phone'],
	allow: [
		{ kind: 'failed_requests', status: 403, url: /\/terminals(\?|$)/, reason: 'the preview cookie may not use terminals (require_terminal_owner refuses preview access by design)' }
	],
	async setup({ page, status, variant }) {
		await gotoApp(page, status);
		await settleServiceWorker(page, status);
		if (variant.viewport === 'phone') await showSidebar(page);
		await page.getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('button', { name: 'Settings', exact: true }).click();
		await page.getByRole('navigation', { name: 'Settings', exact: true }).getByRole('button', { name: label, exact: true }).filter({ visible: true }).first().click();
		await page.getByRole('heading', { name: label, exact: true }).first().waitFor({ timeout: 15_000 });
	},
	async act({ page, mark, variant }) {
		await mark(`reload on Settings > ${label}`);
		// As composer-reload does: the new document's first paint ends this step,
		// so the app shell coming back once it hydrates is the next step's change,
		// not a reversal of this one.
		await page.reload({ waitUntil: 'domcontentloaded' });
		await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => done()))));
		await mark('the page loads its data');
		await page.getByRole('heading', { name: label, exact: true }).first().waitFor({ timeout: 30_000 });
		if (variant.viewport === 'phone') expect(await page.locator('.settings-shell[data-view="page"]').count() === 1, 'the reload stays on the page');
		await page.waitForLoadState('networkidle', { timeout: 5_000 }).catch(() => {});
		// Late answers are what move rows: hold past them.
		await page.waitForTimeout(1500);
	}
});

// Security when its data cannot be read (#1403): the two requests answer 500
// after the first paint, as a worker that errors would. The rows say so in
// their own hints; no notice is inserted between the groups to push them down.
const FAILING = /\/api\/v1\/(owner\/status|settings\/security-instructions)(\?|$)/;
const INSTRUCTIONS = /\/api\/v1\/settings\/security-instructions(\?|$)/;
const rowIn = (page) => (id) => page.locator(`[data-setting-anchor="${id}"]`).filter({ visible: true }).first();
/** Answers matching GETs with 500 about 1.5 s late, as an erroring worker would. */
const failLate = (page, url) =>
	page.context().route(url, async (route) => {
		if (route.request().method() !== 'GET') return route.continue();
		await new Promise((done) => setTimeout(done, 1500));
		await route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: 'Synthetic failure' }) }).catch(() => {});
	});
const focusedText = (page) => page.evaluate(() => document.activeElement?.textContent?.trim() ?? '');
const focusIn = (row) => row.evaluate((el) => el.contains(document.activeElement) && document.activeElement !== document.body);

const failedLoadCase = {
	...reloadCase('Security'),
	name: 'reload-on-security-when-loading-fails',
	allow: [
		...reloadCase('Security').allow,
		{ kind: 'failed_requests', status: 500, url: FAILING, reason: 'this case answers these two requests with 500 on purpose' }
	],
	async setup(ctx) {
		await reloadCase('Security').setup(ctx);
		// The context's route also catches what the service worker fetches.
		await failLate(ctx.page, FAILING);
	},
	async act(ctx) {
		await reloadCase('Security').act(ctx);
		const { page } = ctx;
		const row = rowIn(page);
		expect((await row('security.ownerPassword').locator('.st-rh').textContent()) === 'Could not load (error 500)', 'Owner password says why in its hint');
		expect((await row('security.secInstr').locator('.st-rh').textContent()) === 'Could not load (error 500)', 'Chat instructions says why in its hint');
		expect(await row('security.ownerPassword').getByRole('button', { name: 'Try again' }).isEnabled(), 'Try again is offered in the row');
		expect(await row('security.secInstr').getByRole('button', { name: 'Try again' }).isEnabled(), 'Try again is offered in the instructions row too');
		expect((await page.locator('.settings-shell p.st-msg.err').filter({ visible: true }).count()) === 0, 'no notice is inserted between the groups');

		// From the keyboard: the retry fails, then works. Focus never drops to the page.
		await ctx.mark('Try again, from the keyboard, fails again');
		const retry = row('security.ownerPassword').getByRole('button', { name: /Try again|Retrying/ });
		await retry.focus();
		await page.keyboard.press('Enter');
		await row('security.ownerPassword').getByRole('button', { name: 'Retrying…' }).waitFor({ timeout: 5_000 });
		expect((await focusedText(page)) === 'Retrying…', 'focus stays on the button while it retries');
		await row('security.ownerPassword').getByRole('button', { name: 'Try again' }).waitFor({ timeout: 10_000 });
		expect((await focusedText(page)) === 'Try again', 'focus is still on Try again after a failed retry');
		await page.waitForTimeout(500);

		await ctx.mark('Try again, from the keyboard, works for both rows');
		await page.context().unroute(FAILING);
		await page.keyboard.press(' ');
		await row('security.ownerPassword').locator('.st-rh').filter({ hasNotText: 'Could not load' }).waitFor({ timeout: 10_000 });
		await row('security.secInstr').locator('.st-rh').filter({ hasNotText: 'Could not load' }).waitFor({ timeout: 10_000 });
		expect((await page.getByRole('button', { name: 'Try again' }).count()) === 0, 'no Try again is left');
		expect(await focusIn(row('security.ownerPassword')), 'focus lands in the Owner password row');
		await page.waitForTimeout(1000);
	}
};

// Only the instructions fail: their row offers the retry itself.
const instructionsFailCase = {
	...failedLoadCase,
	name: 'reload-on-security-when-instructions-fail',
	allow: [
		...reloadCase('Security').allow,
		{ kind: 'failed_requests', status: 500, url: INSTRUCTIONS, reason: 'this case answers this request with 500 on purpose' }
	],
	async setup(ctx) {
		await reloadCase('Security').setup(ctx);
		await failLate(ctx.page, INSTRUCTIONS);
	},
	async act(ctx) {
		await reloadCase('Security').act(ctx);
		const { page } = ctx;
		const row = rowIn(page);
		expect((await row('security.secInstr').locator('.st-rh').textContent()) === 'Could not load (error 500)', 'Chat instructions says why in its hint');
		expect((await page.getByRole('button', { name: 'Try again' }).count()) === 1, 'one Try again, in the instructions row');
		expect((await page.locator('.settings-shell p.st-msg.err').filter({ visible: true }).count()) === 0, 'no notice is inserted between the groups');
		await ctx.mark('Try again with the mouse');
		await page.context().unroute(INSTRUCTIONS);
		await row('security.secInstr').getByRole('button', { name: 'Try again' }).click();
		await row('security.secInstr').locator('.st-rh').filter({ hasNotText: 'Could not load' }).waitFor({ timeout: 10_000 });
		expect((await page.getByRole('button', { name: 'Try again' }).count()) === 0, 'the retry recovered the row');
		await page.waitForTimeout(1000);
	}
};

await runJourney({ name: 'settings-reload', allow: DEV_INSTANCE_NOISE, cases: [...pages.map(reloadCase), failedLoadCase, instructionsFailCase] });
