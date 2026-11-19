// Journey: a saved Anthropic API key's Models tab lists what Anthropic's
// Models API returns for THAT key. The dev seed saves two fake keys (A
// selected, B saved; scripts/devloop/seed.py, CLAUDE_API_KEYS) and the app's
// request goes to the fake AI's /anthropic/v1/models (app/core/claude_api_models.py,
// fake_models_url), which pages three at a time and lists a different catalog
// for B, after 3 s (scripts/devloop/fake_wire.py, anthropic_models_reply).
// The Claude mode stays the subscription: preview cookies may not change it,
// so picking and running a version in API-key mode is covered by
// tests/core/test_claude_api_models.py, not here.
//
// Cases:
//   selected-key-lists-and-chooses  key A: its list (all pages), Refresh, and a
//                                   version checked and unchecked again
//   saved-key-own-list-read-only    key B: loading, then B's own list, read-only;
//                                   A's list is unchanged
//   switch-during-refresh           Refresh B, open A before B answers: A shows A's list
//
// The throwaway instance's preview cookie may list models and check versions
// (app/api/auth.py, _preview_may_change_dev_data); a real install's may not.
import { readFileSync } from 'node:fs';
import { runJourney } from './runner.mjs';
import { api, expect, gotoApp, isPhone, showSidebar } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const KEY_A = 'Dev key A';
const KEY_B = 'Dev key B (slow)';
const ONLY_A = 'claude-opus-4-6';
const LAST_A = 'claude-haiku-4-5-20251001'; // On the fake's third page.
const ONLY_B = 'claude-opus-4-1';

/** The fake AI's request log: one JSON line per request, keys redacted. */
const fakeLog = (status) =>
	readFileSync(status.fake_ai.request_log, 'utf8').trim().split('\n').filter(Boolean).map(JSON.parse)
		.filter((e) => e.method === 'GET' && String(e.path).startsWith('/anthropic/v1/models'));

async function accountIds(page) {
	const { accounts } = await api(page, 'GET', '/settings/claude-auth/accounts');
	const byLabel = Object.fromEntries(accounts.api_key.map((a) => [a.label, a]));
	if (!byLabel[KEY_A] || !byLabel[KEY_B]) throw new Error(`the dev seed has no fake API keys: ${JSON.stringify(accounts.api_key.map((a) => a.label))}`);
	return { a: byLabel[KEY_A].id, b: byLabel[KEY_B].id };
}

const setEnabled = (page, enabled) => api(page, 'PUT', '/models/claude/api-enabled', { model: ONLY_A, enabled });

/** Settings > Providers > Claude > the key > Models (also how the journey switches keys). */
async function openKeyModels(page, variant, label) {
	if (isPhone(variant)) await showSidebar(page);
	await page.getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('button', { name: 'Settings', exact: true }).click();
	await page.getByRole('navigation', { name: 'Settings', exact: true }).getByRole('button', { name: 'Providers', exact: true }).click();
	await page.locator('[data-settings-item="provider:claude"]:visible').first().click();
	await page.getByRole('button', { name: `Open ${label}`, exact: true }).first().click();
	await page.getByRole('tab', { name: 'Models', exact: true }).click();
}

const versionsText = async (page) =>
	(await page.getByTestId('claude-api-versioned-models').innerText({ timeout: 15_000 })).replace(/\s+/g, ' ');

/** Another key's Models tab: Back to the Claude page on a phone, the Settings path on desktop. */
async function switchKey(page, variant, label) {
	if (!isPhone(variant)) return openKeyModels(page, variant, label);
	await page.goBack();
	await page.getByRole('button', { name: `Open ${label}`, exact: true }).first().click();
	await page.getByRole('tab', { name: 'Models', exact: true }).click();
}

/** Until the page has stopped asking Anthropic (B answers after 3 s). */
const waitListed = (page) =>
	page.getByTestId('claude-api-models-status').getByText('Listed by Anthropic for this key').waitFor({ timeout: 15_000 });

/** Key B listed before recording: its first listing inserts the list once. */
async function listKeyB(page) {
	const { b } = await accountIds(page);
	await api(page, 'POST', `/models/claude/api-keys/${b}/refresh`, {});
}

await runJourney({
	name: 'claude-api-models',
	// What is listed does not depend on CPU speed.
	throttles: ['fast'],
	cases: [
		{
			name: 'selected-key-lists-and-chooses',
			async setup({ page, status, variant }) {
				await gotoApp(page, status);
				await setEnabled(page, false);
				await openKeyModels(page, variant, KEY_A);
			},
			async act({ page, status, mark, log }) {
				try {
					const { a } = await accountIds(page);
					const listed = await versionsText(page);
					log(`key A lists: ${listed}`);
					expect(listed.includes(ONLY_A) && listed.includes(LAST_A), `key A's list has every page (${listed})`);
					expect(!listed.includes(ONLY_B), `key A's list has none of key B's models (${listed})`);
					expect((await page.getByTestId('claude-api-alias-models').innerText()).includes('opus'), 'the aliases stay listed');

					await mark('refresh key A');
					const before = fakeLog(status).length;
					const refreshed = page.waitForResponse((r) => r.url().includes('/refresh') && r.request().method() === 'POST', { timeout: 15_000 });
					await page.getByRole('button', { name: 'Refresh model list' }).click();
					await refreshed;
					await waitListed(page);
					const asked = fakeLog(status).slice(before);
					log(`refresh asked: ${asked.map((e) => e.path).join(' ')}`);
					expect(asked.length >= 3 && asked.some((e) => e.path.includes('after_id=')), 'Refresh pages through the list');
					const headers = Object.fromEntries(Object.entries(asked[0].headers).map(([k, v]) => [k.toLowerCase(), v]));
					expect(headers['anthropic-version'] === '2023-06-01', `anthropic-version sent (${headers['anthropic-version']})`);
					expect(headers['x-api-key'] === '<redacted>', 'the key was sent as x-api-key');
					expect((await page.locator('.pv-msg.err[role="alert"]').count()) === 0, 'no refresh error');

					await mark(`check ${ONLY_A}`);
					const box = page.getByTestId('claude-api-versioned-models').getByRole('checkbox', { name: new RegExp(ONLY_A) });
					expect(!(await box.isChecked()), `${ONLY_A} is off by default`);
					await box.click();
					await page.waitForFunction(
						async ({ id, value }) => (await (await fetch(`/api/v1/models/claude/api-keys/${id}`, { credentials: 'include' })).json()).enabled.includes(value),
						{ id: a, value: ONLY_A }, { timeout: 10_000, polling: 500 }
					);
					expect(await box.isChecked(), `${ONLY_A} reads on after saving`);

					await mark(`uncheck ${ONLY_A}`);
					await box.click();
					await page.waitForFunction(
						async ({ id, value }) => !(await (await fetch(`/api/v1/models/claude/api-keys/${id}`, { credentials: 'include' })).json()).enabled.includes(value),
						{ id: a, value: ONLY_A }, { timeout: 10_000, polling: 500 }
					);
					expect(!(await box.isChecked()), `${ONLY_A} reads off after saving`);
				} finally {
					await setEnabled(page, false).catch(() => {});
				}
			}
		},
		{
			name: 'saved-key-own-list-read-only',
			async setup({ page, status, variant }) {
				await gotoApp(page, status);
				await listKeyB(page);
				await openKeyModels(page, variant, KEY_A);
				await page.getByTestId('claude-api-versioned-models').waitFor({ timeout: 15_000 });
			},
			async act({ page, variant, mark, log }) {
				const { a, b } = await accountIds(page);
				await mark('open key B');
				await switchKey(page, variant, KEY_B);
				await mark('refresh key B (answers after 3 s)');
				await page.getByRole('button', { name: 'Refresh model list' }).click();
				await page.getByTestId('claude-api-models-status').getByText('Asking Anthropic which models this key can use').waitFor({ timeout: 5000 });
				expect(await page.getByRole('button', { name: 'Refresh model list' }).isDisabled(), 'Refresh is off while asking');
				await waitListed(page);
				const shown = await versionsText(page);
				log(`key B lists: ${shown}`);
				expect(shown.includes(ONLY_B) && !shown.includes(ONLY_A), `key B shows its own list (${shown})`);
				const boxes = page.getByTestId('claude-api-versioned-models').getByRole('checkbox');
				expect((await boxes.count()) > 0 && await boxes.first().isDisabled(), 'an unselected key cannot change the pickers');
				await page.getByText('Select this key to choose which ones the model pickers offer.').waitFor();
				const keyA = await api(page, 'GET', `/models/claude/api-keys/${a}`);
				const keyB = await api(page, 'GET', `/models/claude/api-keys/${b}`);
				expect(keyA.active && !keyB.active, 'A stays the selected key');
				const aIds = keyA.catalog.models.map((m) => m.value);
				expect(aIds.includes(ONLY_A) && !aIds.includes(ONLY_B), `key A's list is unchanged (${aIds})`);
			}
		},
		{
			name: 'switch-during-refresh',
			async setup({ page, status, variant }) {
				await gotoApp(page, status);
				await listKeyB(page);
				await openKeyModels(page, variant, KEY_B);
				await page.getByTestId('claude-api-versioned-models').waitFor({ timeout: 15_000 });
			},
			async act({ page, variant, mark, log }) {
				await mark('refresh key B, open key A before B answers');
				await page.getByRole('button', { name: 'Refresh model list' }).click();
				await switchKey(page, variant, KEY_A);
				const first = await versionsText(page);
				await page.waitForTimeout(3500); // B's reply has arrived meanwhile.
				const after = await versionsText(page);
				log(`key A after B answered: ${after}`);
				for (const text of [first, after]) {
					expect(text.includes(ONLY_A) && !text.includes(ONLY_B), `key A's page shows only A's list (${text})`);
				}
				expect((await page.locator('.pv-msg.err[role="alert"]').count()) === 0, 'no error on key A');
			}
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
});
