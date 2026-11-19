// Real settings flow against the dev instance's paged Anthropic Models API.
import { runJourney } from './runner.mjs';
import { gotoApp, isPhone, showSidebar } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

await runJourney({
	name: 'claude-api-models',
	throttles: ['fast'],
	allow: DEV_INSTANCE_NOISE,
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
			await page.getByText('claude-haiku-dev-api', { exact: true }).waitFor({ timeout: 10_000 });
		}
	}]
});
