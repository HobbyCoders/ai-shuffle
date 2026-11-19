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
		}
	}]
});
