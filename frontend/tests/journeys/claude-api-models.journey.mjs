// Real settings flow against the dev instance's paged Anthropic Models API:
// curation checkboxes, filter box, non-looping refresh on the active key,
// persistence across Settings reopen, real Chat model picker visibility,
// and long-name wrapping.
import { runJourney } from './runner.mjs';
import { expect, gotoApp, isPhone, openPopup, showSidebar, waitChat } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

async function activateAnthropicApiKey(status) {
	const localPort = status?.app?.local_port;
	if (!localPort) throw new Error('devloop status missing app.local_port');
	const origin = `http://127.0.0.1:${localPort}`;
	const headers = { Origin: origin, 'Sec-Fetch-Site': 'same-origin' };
	const listRes = await fetch(`${origin}/api/v1/settings/claude-auth/accounts`, { headers });
	if (!listRes.ok) throw new Error(`list accounts failed: ${listRes.status}`);
	const { accounts } = await listRes.json();
	const key = accounts?.api_key?.[0];
	if (!key?.id) throw new Error('devloop has no seeded Anthropic API key');
	if (!key.active) {
		const actRes = await fetch(
			`${origin}/api/v1/settings/claude-auth/accounts/api_key/${encodeURIComponent(key.id)}/activate`,
			{ method: 'POST', headers }
		);
		if (!actRes.ok) throw new Error(`activate API key failed: ${actRes.status}`);
	}
	// If a prior interrupted run left curation narrowed, restore both dev models
	// without warming a cold catalog cache (GET /api/v1/models only reads cached state).
	const regRes = await fetch(`${origin}/api/v1/models`, { headers });
	if (regRes.ok) {
		const reg = await regRes.json();
		const customModels = (reg.custom_providers ?? []).flatMap((p) => p.models ?? []);
		const hasSonnet = customModels.some((m) => m.id === 'claude-sonnet-dev-api');
		const hasHaiku = customModels.some((m) => m.id === 'claude-haiku-dev-api');
		if (hasSonnet && !hasHaiku) {
			await fetch(`${origin}/api/v1/models/claude/api-key/${encodeURIComponent(key.id)}/models`, {
				method: 'PUT',
				headers: { ...headers, 'Content-Type': 'application/json' },
				body: JSON.stringify({ models: ['claude-sonnet-dev-api', 'claude-haiku-dev-api'] })
			});
		}
	}
	return key.id;
}

async function resetAnthropicApiKeyCuration(status, keyId) {
	const localPort = status?.app?.local_port;
	if (!localPort || !keyId) return;
	const origin = `http://127.0.0.1:${localPort}`;
	await fetch(`${origin}/api/v1/models/claude/api-key/${encodeURIComponent(keyId)}/models`, {
		method: 'PUT',
		headers: { Origin: origin, 'Sec-Fetch-Site': 'same-origin', 'Content-Type': 'application/json' },
		body: JSON.stringify({ models: ['claude-sonnet-dev-api', 'claude-haiku-dev-api'] })
	}).catch(() => {});
}

async function closeModelPicker(page, picker) {
	// First Escape clears a non-empty search box; second Escape closes the dialog.
	await page.keyboard.press('Escape');
	if (await picker.isVisible()) await page.keyboard.press('Escape');
	await picker.waitFor({ state: 'hidden', timeout: 5_000 });
}

async function openAnthropicApiKey(page, variant) {
	if (isPhone(variant)) await showSidebar(page);
	await page.getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('button', { name: 'Settings', exact: true }).click();
	await page.getByRole('navigation', { name: 'Settings', exact: true }).getByRole('button', { name: 'Providers', exact: true }).click();
	await page.locator('[data-settings-item="provider:claude"]:visible').first().click();
	await page.getByRole('button', { name: 'Open Anthropic API key', exact: true }).click();
}

async function openChatModelPicker(page, variant) {
	if (isPhone(variant)) {
		const backToChats = page.getByRole('button', { name: 'Back to chats', exact: true });
		for (let i = 0; i < 4 && !(await backToChats.isVisible()); i++) {
			const backBtn = page.getByRole('button', { name: /^Back to / }).first();
			if (await backBtn.isVisible()) await backBtn.click();
			else break;
		}
	}
	await page.getByRole('button', { name: 'Back to chats', exact: true }).click();
	await waitChat(page, { surface: 'new' }, { timeout: 30_000 });
	const picker = page.getByRole('dialog', { name: 'Choose model' });
	await openPopup(page.getByRole('button', { name: /^Model:/ }).first(), picker);
	return picker;
}

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
			status._claudeApiKeyId = await activateAnthropicApiKey(status);
			await gotoApp(page, status);
			await openAnthropicApiKey(page, variant);
		},
		async act({ page, status, mark, variant }) {
			let apiGetCount = 0;
			const onRequest = (req) => {
				if (req.method() === 'GET' && req.url().includes('/api/v1/models/claude/api-key/')) apiGetCount += 1;
			};
			page.on('request', onRequest);
			try {
				await mark('open the active saved key’s Models tab');
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

				// Cold-cache mount must settle quickly with no refresh loop over 2.5s
				// (allow at most 1 background sync_reconcile tick; a loop fires dozens of GETs).
				await page.waitForTimeout(500);
				const settledGets = apiGetCount;
				expect(settledGets <= 2, `expected at most initial GET + 1 cached settings echo (saw ${settledGets})`);
				await page.waitForTimeout(2_000);
				expect(apiGetCount <= settledGets + 1 && apiGetCount <= 3,
					`expected catalog fetches to stay settled while idle (saw ${settledGets} -> ${apiGetCount})`);

				await mark('filter models and clear empty match');
				const filterInput = panel.getByRole('searchbox', { name: 'Filter models', exact: true });
				await filterInput.fill('haiku');
				expect(await rows.count() === 1, 'filter narrows the model list');
				await filterInput.fill('no-such-model');
				await panel.getByRole('button', { name: 'Clear', exact: true }).click();
				expect(await rows.count() === 2, 'Clear restores all model rows');

				await mark('uncheck a model and verify picker + persistence across reopen');
				const saveUncheck = page.waitForResponse((res) =>
					res.request().method() === 'PUT' && res.url().includes('/api/v1/models/claude/api-key/') && res.url().endsWith('/models'));
				await checkboxes.nth(1).click();
				expect((await saveUncheck).ok(), 'saving unchecked Claude API curation must succeed');
				expect(!(await checkboxes.nth(1).isChecked()), 'unchecked model stays unchecked');
				await panel.getByText('1 of 2', { exact: true }).waitFor({ timeout: 5_000 });

				// Verify in the real Chat model picker that Haiku (dev API) is hidden while Sonnet (dev API) stays offered.
				await mark('verify Chat model picker hides unchecked model');
				const pickerHidden = await openChatModelPicker(page, variant);
				const searchHidden = pickerHidden.getByRole('textbox', { name: 'Search models' });
				await searchHidden.fill('dev API');
				await pickerHidden.getByRole('option', { name: /Claude Sonnet \(dev API\)/ }).first().waitFor({ timeout: 5_000 });
				expect(await pickerHidden.getByRole('option', { name: /Claude Haiku \(dev API\)/ }).count() === 0,
					'unchecked Claude API model must be hidden in the Chat model picker');
				await closeModelPicker(page, pickerHidden);

				// Reopen Settings and confirm the unchecked state persisted.
				await mark('reopen Settings and confirm unchecked persistence');
				await openAnthropicApiKey(page, variant);
				await page.getByRole('tab', { name: 'Models', exact: true }).click();
				const reopenedPanel = page.getByRole('tabpanel', { name: 'Models' });
				const reopenedList = reopenedPanel.getByTestId('claude-api-models');
				await reopenedPanel.getByText('1 of 2', { exact: true }).waitFor({ timeout: 10_000 });
				const reopenedCheckboxes = reopenedList.getByRole('checkbox');
				expect(await reopenedCheckboxes.nth(0).isChecked(), 'checked model stays checked after reopening Settings');
				expect(!(await reopenedCheckboxes.nth(1).isChecked()), 'unchecked model stays unchecked after reopening Settings');

				await mark('re-check the model');
				const saveRecheck = page.waitForResponse((res) =>
					res.request().method() === 'PUT' && res.url().includes('/api/v1/models/claude/api-key/') && res.url().endsWith('/models'));
				await reopenedCheckboxes.nth(1).click();
				expect((await saveRecheck).ok(), 're-enabling Claude API model must succeed');
				expect(await reopenedCheckboxes.nth(1).isChecked(), 're-checked model is checked again');
				await reopenedPanel.getByText('2 of 2', { exact: true }).waitFor({ timeout: 5_000 });

				await mark('confirm re-checked model returns to Chat picker');
				const pickerRestored = await openChatModelPicker(page, variant);
				await pickerRestored.getByRole('textbox', { name: 'Search models' }).fill('dev API');
				await pickerRestored.getByRole('option', { name: /Claude Haiku \(dev API\)/ }).first().waitFor({ timeout: 5_000 });
				await closeModelPicker(page, pickerRestored);

				// Return to the Models tab for refresh / rejection / wrapping checks.
				await mark('return to Models tab for refresh checks');
				await openAnthropicApiKey(page, variant);
				await page.getByRole('tab', { name: 'Models', exact: true }).click();
				const finalPanel = page.getByRole('tabpanel', { name: 'Models' });
				const finalList = finalPanel.getByTestId('claude-api-models');
				await finalPanel.getByText('claude-haiku-dev-api', { exact: true }).waitFor({ timeout: 10_000 });

				await mark('refresh the model list');
				const refresh = finalPanel.getByRole('button', { name: 'Refresh model list', exact: true });
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
				const retryResp = page.waitForResponse((res) =>
					res.request().method() === 'GET' && res.url().includes('/api/v1/models/claude/api-key/') && res.url().includes('refresh=true'));
				await refresh.click();
				expect((await retryResp).ok(), 'retry after mocked 502 must succeed against real dev endpoint');
				expect(await page.getByRole('alert').filter({ hasText: rejection }).count() === 0, 'retry clears the alert');
				await page.getByText('claude-haiku-dev-api', { exact: true }).waitFor({ timeout: 10_000 });

				const longModel = { id: `claude-${'long-id-'.repeat(30)}dev`, label: `Claude ${'LongDisplayName'.repeat(16)}` };
				await page.route('**/api/v1/models/claude/api-key/*', (route) => route.fulfill({
					json: { models: [longModel], curated: null, fetched_at: '2026-11-20T00:00:00Z' }
				}), { times: 1 });
				await mark('refresh with a long id and display name');
				await refresh.click();
				await page.getByText(longModel.id, { exact: true }).waitFor({ timeout: 10_000 });
				await page.getByText(longModel.label, { exact: true }).waitFor({ timeout: 10_000 });
				expect(await finalList.evaluate((el) => el.clientWidth >= el.scrollWidth &&
					[...el.querySelectorAll('.id, .lbl')].every((cell) => cell.clientWidth >= cell.scrollWidth)),
					'long ids and display names must wrap inside the model list without clipping');
			} finally {
				page.off('request', onRequest);
				await resetAnthropicApiKeyCuration(status, status._claudeApiKeyId);
			}
		}
	}]
});
