// Real settings flow against the dev instance's paged Anthropic Models API.
import { runJourney } from './runner.mjs';
import { expect, gotoApp, isPhone, showSidebar } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

await runJourney({
	name: 'claude-api-models',
	// The built app’s service worker handles GETs before Playwright can inject the rejected refresh.
	// Keep interception in this journey only; the successful requests still reach the app.
	browser: async () => ({ context: { serviceWorkers: 'block' } }),
	throttles: ['fast'],
	allow: [...DEV_INSTANCE_NOISE, {
		kind: 'failed_requests', status: 502, url: /\/models\/claude\/api-key\/[^/]+$/,
		reason: 'the journey injects one rejected-key response; it asserts the error and then retries against the real dev endpoint'
	}],
	cases: [{
		name: 'saved-key-catalog',
		async setup({ page, status, variant }) {
			await gotoApp(page, status);
			if (isPhone(variant)) await showSidebar(page);
			await page.getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('button', { name: 'Settings', exact: true }).click();
			await page.getByRole('navigation', { name: 'Settings', exact: true }).getByRole('button', { name: 'Providers', exact: true }).click();
			await page.locator('[data-settings-item="provider:claude"]:visible').first().click();
			await page.getByRole('button', { name: 'Open Anthropic API key', exact: true }).click();
		},
		async act({ page, mark }) {
			await mark('open the saved key’s Models tab');
			await page.getByRole('tab', { name: 'Models', exact: true }).click();
			await page.getByText('claude-sonnet-dev-api', { exact: true }).waitFor({ timeout: 10_000 });
			await page.getByText('claude-haiku-dev-api', { exact: true }).waitFor({ timeout: 10_000 });
			const list = page.getByRole('tabpanel', { name: 'Models' }).locator('.st-card .pv-mlist');
			const rows = list.locator('.pv-mrow.static');
			expect(await rows.count() === 2, 'both paged API results must use the read-only model rows');
			for (const [index, id, label] of [
				[0, 'claude-sonnet-dev-api', 'Claude Sonnet (dev API)'],
				[1, 'claude-haiku-dev-api', 'Claude Haiku (dev API)']
			]) {
				const row = rows.nth(index);
				expect(await row.locator('.id').textContent() === id, 'keep the catalog id and ordering');
				expect(await row.locator('.lbl').textContent() === label, 'keep the API display name beside its id');
				expect(await row.locator(':scope > .st-dot.ok + .id + .lbl').count() === 1,
					'use the shared read-only list design: availability dot, model id, then label');
			}
			expect(await list.getByRole('checkbox').count() === 0, 'the API catalog must not become a picker checklist');
			await mark('refresh the model list');
			const response = page.waitForResponse((res) => res.url().includes('/api/v1/models/claude/api-key/'));
			await page.getByRole('button', { name: 'Refresh models', exact: true }).click();
			if (!(await response).ok()) throw new Error('Refreshing the Anthropic model list failed');
			const refresh = page.getByRole('button', { name: 'Refresh models', exact: true });
			const model = page.getByText('claude-haiku-dev-api', { exact: true });
			await model.waitFor({ timeout: 10_000 });

			const rejection = 'Anthropic did not accept this API key for listing models.';
			await page.route('**/api/v1/models/claude/api-key/*', (route) => route.fulfill({
				status: 502, json: { detail: rejection }
			}), { times: 1 });
			await mark('refresh after Anthropic rejects the key');
			await refresh.click();
			await page.getByRole('alert').filter({ hasText: rejection }).waitFor({ timeout: 10_000 });
			expect(!(await model.isVisible()), 'a rejected refresh must not leave previously listed models visible');

			await mark('retry after the rejected refresh');
			await refresh.click();
			await model.waitFor({ timeout: 10_000 });

			const longModel = { id: `claude-${'long-id-'.repeat(30)}dev`, label: `Claude ${'LongDisplayName'.repeat(16)}` };
			await page.route('**/api/v1/models/claude/api-key/*', (route) => route.fulfill({
				json: { models: [longModel] }
			}), { times: 1 });
			await mark('refresh with a long id and display name');
			await refresh.click();
			await page.getByText(longModel.id, { exact: true }).waitFor({ timeout: 10_000 });
			await page.getByText(longModel.label, { exact: true }).waitFor({ timeout: 10_000 });
			expect(await list.evaluate((el) => el.clientWidth >= el.scrollWidth &&
				[...el.querySelectorAll('.id, .lbl')].every((cell) => cell.clientWidth >= cell.scrollWidth)),
				'long ids and display names must wrap inside the model list without clipping');
		}
	}]
});
