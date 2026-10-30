// Settings > Providers: two saved accounts in one slot, one active marker.
// Like mirrored-chat, seed public API shapes in window.fetch so the built app's
// service worker cannot hide requests from the fixture. Preview grants deliberately
// refuse credential mutations: this covers the real UI/store/activate+refetch flow,
// not OAuth, encrypted storage or backend slot selection (covered by Python tests).
import { runJourney } from './runner.mjs';
import { expect, gotoApp, seededChatId, showSidebar, waitChatOpen } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const ACCOUNTS = [
	{ id: 'journey-account-a', name: 'Journey work key', enabled: true },
	{ id: 'journey-account-b', name: 'Journey personal key', enabled: false }
].map((account) => ({
	...account,
	provider_type: 'openrouter', slot: 'openrouter:key',
	base_url: 'https://fixture.invalid', auth_token_set: true,
	auth_token_masked: 'fixture-only', models: [], default_model: null,
	small_fast_model: null, extra_env: {}, context_window: null,
	max_output_tokens: null, created_at: null, updated_at: null
}));

async function seedSavedAccounts(page) {
	await page.addInitScript((initial) => {
		const accounts = structuredClone(initial);
		const original = window.fetch.bind(window);
		window.__providerSwitchRequests = [];
		const json = (body) => new Response(JSON.stringify(body), {
			status: 200, headers: { 'content-type': 'application/json' }
		});
		window.fetch = async (input, init) => {
			const path = new URL(typeof input === 'string' ? input : input.url, location.href).pathname;
			const method = (init?.method || input?.method || 'GET').toUpperCase();
			const activate = accounts.find((a) => path === `/api/v1/model-providers/${a.id}/activate`);
			if (method === 'POST' && activate) {
				window.__providerSwitchRequests.push({ id: activate.id, method });
				await new Promise((resolve) => setTimeout(resolve, 200));
				for (const account of accounts) account.enabled = account.id === activate.id;
				return json(activate); // The real endpoint returns only the selected row.
			}
			const response = await original(input, init);
			if (method !== 'GET' || path !== '/api/v1/model-providers' || !response.ok) return response;
			const body = await response.json();
			return json({ ...body, providers: [...body.providers, ...accounts] });
		};
	}, ACCOUNTS);
}

const row = (page, id) => page.locator(`[data-settings-item="mp:${id}"]`);

async function assertSelection(page, selected, saved) {
	await row(page, selected).getByRole('switch', { checked: true }).waitFor();
	await row(page, saved).getByRole('switch', { checked: false }).waitFor();
	await row(page, selected).getByText('Active', { exact: true }).waitFor();
	expect(await row(page, saved).getByText('Active', { exact: true }).count() === 0,
		'the inactive sibling has no Active marker');
}

await runJourney({
	name: 'provider-account-switch',
	cases: [{
		name: 'saved-api-key-slot',
		async setup({ page, status }) {
			await seedSavedAccounts(page);
			await gotoApp(page, status, `/?session=${encodeURIComponent(seededChatId(status, 'no_project'))}`);
			await waitChatOpen(page, { messagesAtLeast: 2, hold: 800 });
			// Stage navigation and async integration loading before judging the switch.
			const sidebar = await showSidebar(page);
			await sidebar.getByRole('navigation', { name: 'Workspace' })
				.getByRole('button', { name: 'Settings', exact: true }).click();
			const settings = page.getByRole('navigation', { name: 'Settings', exact: true });
			await settings.waitFor();

			await settings.getByRole('button', { name: 'Providers', exact: true }).click();
			await assertSelection(page, 'journey-account-a', 'journey-account-b');
			await page.waitForTimeout(1000);
		},
		async act({ page, mark }) {
			await mark('activate the saved sibling');
			await row(page, 'journey-account-b').getByRole('switch', { name: 'Use Journey personal key', exact: true }).click();
			await assertSelection(page, 'journey-account-b', 'journey-account-a');
			expect(JSON.stringify(await page.evaluate(() => window.__providerSwitchRequests)) ===
				JSON.stringify([{ id: 'journey-account-b', method: 'POST' }]),
				'exactly one activation, followed by a list refetch, moves the marker');
			await page.waitForTimeout(1000);
		}
	}],
	allow: DEV_INSTANCE_NOISE
});
