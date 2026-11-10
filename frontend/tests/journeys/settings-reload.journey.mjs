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
		await ctx.page.context().route(FAILING, async (route) => {
			if (route.request().method() !== 'GET') return route.continue();
			await new Promise((done) => setTimeout(done, 1500));
			await route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: 'Synthetic failure' }) }).catch(() => {});
		});
	},
	async act(ctx) {
		await reloadCase('Security').act(ctx);
		const { page } = ctx;
		const row = (id) => page.locator(`[data-setting-anchor="${id}"]`).filter({ visible: true }).first();
		expect((await row('security.ownerPassword').locator('.st-rh').textContent()) === 'Could not load (error 500)', 'Owner password says why in its hint');
		expect((await row('security.secInstr').locator('.st-rh').textContent()) === 'Could not load (error 500)', 'Chat instructions says why in its hint');
		expect(await row('security.ownerPassword').getByRole('button', { name: 'Try again' }).isEnabled(), 'Try again is offered in the row');
		expect((await page.locator('.settings-shell p.st-msg.err').filter({ visible: true }).count()) === 0, 'no notice is inserted between the groups');
	}
};

await runJourney({ name: 'settings-reload', allow: DEV_INSTANCE_NOISE, cases: [...pages.map(reloadCase), failedLoadCase] });
