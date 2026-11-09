// Journey: the Agents page's autosave, through the preview cookie a dev
// instance is reached with (#989). Pin a model on an agent's Models tab, wait
// for it to say Saved, reload, and read the saved pin back from the page.
//
// Only a throwaway instance's cookie may save an agent; on an installed worker
// the same cookie stays read-only (tests/api/test_preview_project_management.py).
import { runJourney } from './runner.mjs';
import { api, expect, gotoApp, isPhone, showSidebar } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const AGENT = 'dev-agent';
const PINNED = 'fake-ai-safe';
const PINNED_LABEL = 'Fake Ai Safe';

function fakeProviderRow(page) {
	return page.locator('.st-row').filter({ hasText: 'Fake AI (dev)' });
}

async function agentOnDisk(page) {
	return api(page, 'GET', `/agents/${AGENT}`);
}

/**
 * The Dev agent is the default for every other journey on this seed: whatever
 * the save does to its config, put the seeded config back, pass or fail.
 *
 * The seed has no streaming setting, which runs as on, but the read API reports
 * it as off (#1314). Writing that back would stop every later journey's reply
 * from streaming, so name the value the agent actually runs with.
 */
async function restoreAgent(page, seeded) {
	const config = { ...seeded.config, include_partial_messages: true };
	await api(page, 'PUT', `/agents/${AGENT}`, { name: seeded.name, description: seeded.description, config });
}

async function openAgentModels(page, variant) {
	if (isPhone(variant)) await showSidebar(page);
	await page.getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('button', { name: 'Agents', exact: true }).click();
	await page.locator('[data-settings-item]').filter({ hasText: 'Dev agent' }).first().click();
	await page.getByRole('tab', { name: /^Models/ }).click();
	await page.getByRole('radiogroup', { name: 'Model for this agent' }).waitFor();
}

/** After a reload the app is back on the Agents list or on this agent: reach its Models tab either way. */
async function reopenAgentModels(page, variant) {
	const models = page.getByRole('tab', { name: /^Models/ });
	const row = page.locator('[data-settings-item]').filter({ hasText: 'Dev agent' }).first();
	await models.or(row).first().waitFor({ timeout: 90_000 });
	if (!(await models.isVisible())) {
		if (isPhone(variant) && !(await row.isVisible())) await showSidebar(page);
		await row.click();
	}
	await models.click();
	await page.getByRole('radiogroup', { name: 'Model for this agent' }).waitFor();
}

let seeded;

await runJourney({
	name: 'agent-model-pin',
	cases: [{
		name: 'pin-saves-and-reopens',
		async setup({ page, status, variant }) {
			await gotoApp(page, status);
			seeded = await agentOnDisk(page);
			await openAgentModels(page, variant);
		},
		async act({ page, mark }) {
			try {
				await mark('pin a model on the agent');
				await page.locator('[data-mode="pinned"]').click();
				await fakeProviderRow(page).locator('.mpk-trigger').click();
				await page.locator('.mpp-row-label').getByText(PINNED_LABEL, { exact: true }).click();
				await page.getByRole('status').filter({ hasText: 'Saved' }).waitFor({ timeout: 15_000 });
			} catch (error) {
				await restoreAgent(page, seeded).catch(() => {});
				throw error;
			}
		},
		async verify({ page, variant, log }) {
			try {
				const saved = await agentOnDisk(page);
				const value = Object.values(saved.config.provider_models ?? {}).join(' ');
				expect(value.includes(PINNED), `the save reached the server: ${value}`);
				// The app reopens on the view it was left on, so a reload lands back here.
				await page.reload();
				await reopenAgentModels(page, variant);
				expect(await page.locator('[data-mode="pinned"]').getAttribute('aria-checked') === 'true', 'the reopened agent is pinned');
				const shown = (await fakeProviderRow(page).locator('.mpk-trigger').innerText()).trim();
				expect(shown === PINNED_LABEL, `the reopened agent shows the saved model: ${shown}`);
				log(`Saved through the preview cookie and read back: ${value}`);
			} finally {
				await restoreAgent(page, seeded);
			}
		}
	}],
	allow: [...DEV_INSTANCE_NOISE]
});
