// Real settings flow against the dev instance's paged Anthropic Models API:
// curation checkboxes, filter box, non-looping refresh, and long-name wrapping.
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
		kind: 'failed_requests', status: 502, url: /\/models\/claude\/api-key\/[^/?]+/,
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
			let apiGetCount = 0;
			const onRequest = (req) => {
				if (req.method() === 'GET' && req.url().includes('/api/v1/models/claude/api-key/')) apiGetCount += 1;
			};
			page.on('request', onRequest);
			try {
				await mark('open the saved key’s Models tab');
				await page.getByRole('tab', { name: 'Models', exact: true }).click();
				await page.getByText('claude-sonnet-dev-api', { exact: true }).waitFor({ timeout: 10_000 });
				await page.getByText('claude-haiku-dev-api', { exact: true }).waitFor({ timeout: 10_000 });
				const panel = page.getByRole('tabpanel', { name: 'Models' });
				const list = panel.getByTestId('claude-api-models');
				const rows = list.locator('.pv-mrow');
				expect(await rows.count() === 2, 'both paged API results must render as curated rows');
				const checkboxes = list.getByRole('checkbox');
				expect(await checkboxes.count() === 2, 'each Claude API model row must have a checkbox');
				expect(await rows.filter({ has: page.locator('[data-provider-logo="claude"]:visible') }).count() === 2,
					'every Claude API model row shows the Claude glyph');

				// Confirm the Models tab does NOT enter an infinite refresh loop.
				const initialGets = apiGetCount;
				await page.waitForTimeout(800);
				expect(apiGetCount === initialGets, `expected no repeated catalog fetches while idle (saw ${initialGets} -> ${apiGetCount})`);

				await mark('filter models and clear empty match');
				const filterInput = panel.getByRole('searchbox', { name: 'Filter models', exact: true });
				await filterInput.fill('haiku');
				expect(await rows.count() === 1, 'filter narrows the model list');
				await filterInput.fill('no-such-model');
				await panel.getByRole('button', { name: 'Clear', exact: true }).click();
				expect(await rows.count() === 2, 'Clear restores all model rows');

				await mark('uncheck and re-check a model');
				const saveUncheck = page.waitForResponse((res) =>
					res.request().method() === 'PUT' && res.url().includes('/api/v1/models/claude/api-key/') && res.url().endsWith('/models'));
				await checkboxes.nth(1).click();
				expect((await saveUncheck).ok(), 'saving unchecked Claude API curation must succeed');
				expect(!(await checkboxes.nth(1).isChecked()), 'unchecked model stays unchecked');
				await panel.getByText('1 of 2', { exact: true }).waitFor({ timeout: 5_000 });

				const saveRecheck = page.waitForResponse((res) =>
					res.request().method() === 'PUT' && res.url().includes('/api/v1/models/claude/api-key/') && res.url().endsWith('/models'));
				await checkboxes.nth(1).click();
				expect((await saveRecheck).ok(), 're-enabling Claude API model must succeed');
				expect(await checkboxes.nth(1).isChecked(), 're-checked model is checked again');
				await panel.getByText('2 of 2', { exact: true }).waitFor({ timeout: 5_000 });

				await mark('refresh the model list');
				const refresh = panel.getByRole('button', { name: 'Refresh model list', exact: true });
				const refreshResp = page.waitForResponse((res) =>
					res.request().method() === 'GET' && res.url().includes('/api/v1/models/claude/api-key/') && res.url().includes('refresh=true'));
				await refresh.click();
				if (!(await refreshResp).ok()) throw new Error('Refreshing the Anthropic model list failed');
				await page.getByText('claude-haiku-dev-api', { exact: true }).waitFor({ timeout: 10_000 });

				const rejection = 'Anthropic did not accept this API key for listing models.';
				await page.route('**/api/v1/models/claude/api-key/*', (route) => route.fulfill({
					status: 502, json: { detail: rejection }
				}), { times: 1 });
				await mark('refresh after Anthropic rejects the key');
				await refresh.click();
				await page.getByRole('alert').filter({ hasText: rejection }).waitFor({ timeout: 10_000 });
				// Saved curation remains visible and checked on failed refresh so nothing is wiped.
				expect(await page.getByText('claude-haiku-dev-api', { exact: true }).isVisible(),
					'saved curation must remain visible when a refresh fails');

				await mark('retry after the rejected refresh');
				await refresh.click();
				await page.getByText('claude-haiku-dev-api', { exact: true }).waitFor({ timeout: 10_000 });

				const longModel = { id: `claude-${'long-id-'.repeat(30)}dev`, label: `Claude ${'LongDisplayName'.repeat(16)}` };
				await page.route('**/api/v1/models/claude/api-key/*', (route) => route.fulfill({
					json: { models: [longModel], curated: null, fetched_at: '2026-11-20T00:00:00Z' }
				}), { times: 1 });
				await mark('refresh with a long id and display name');
				await refresh.click();
				await page.getByText(longModel.id, { exact: true }).waitFor({ timeout: 10_000 });
				await page.getByText(longModel.label, { exact: true }).waitFor({ timeout: 10_000 });
				expect(await list.evaluate((el) => el.clientWidth >= el.scrollWidth &&
					[...el.querySelectorAll('.id, .lbl')].every((cell) => cell.clientWidth >= cell.scrollWidth)),
					'long ids and display names must wrap inside the model list without clipping');
			} finally {
				page.off('request', onRequest);
			}
		}
	}]
});
