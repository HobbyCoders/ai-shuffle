// The Python regression covers the raw /usage reply. Feed the browser the
// resulting public snapshot to exercise labels and wrapping in both layouts.
import { runJourney } from './runner.mjs';
import { expect, gotoApp, seededChatId, waitChatOpen } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const quotas = [
	['gemini-weekly', 'Gemini Models, weekly'],
	['gemini-5h', 'Gemini Models, 5-hour'],
	['3p-weekly', 'Claude and GPT models, weekly'],
	['3p-5h', 'Claude and GPT models, 5-hour'],
	['gemini-argon-wtu-weekly', 'Gemini 4 Argon, weekly']
];

await runJourney({
	name: 'antigravity-usage',
	cases: [{
		name: 'five-quotas',
		async setup({ page, status }) {
			await page.addInitScript((quotas) => {
				const fetch = window.fetch.bind(window);
				window.fetch = (input, init) => {
					const url = new URL(input instanceof Request ? input.url : input, location.href);
					if (url.pathname !== '/api/v1/usage/limits') return fetch(input, init);
					const now = Date.now();
					return Promise.resolve(Response.json({ providers: [{
						provider_id: 'fixture-agy', provider_name: 'Fixture Antigravity',
						provider_type: 'antigravity', status: 'fresh', fetched_at: now,
						windows: quotas.map(([id, label]) => ({
							type: `antigravity_${id}`, label, utilization: 25,
							resets_at: new Date(now + 3600_000).toISOString()
						}))
					}] }));
				};
			}, quotas);
			await gotoApp(page, status, `/?session=${encodeURIComponent(seededChatId(status, 'no_project'))}`);
			await waitChatOpen(page, { messagesAtLeast: 2 });
		},
		async act({ page, mark }) {
			await mark('open the usage meters');
			const controls = page.getByRole('button', { name: 'All chat controls' });
			if (await controls.isVisible()) {
				await controls.click();
				await page.getByRole('button', { name: /^Context and usage:/ }).click();
			} else {
				await page.getByRole('button', { name: /^Context and usage —/ }).click();
			}
			const panel = page.locator('.usage-popover');
			const provider = panel.getByRole('button', { name: /^Antigravity.*usage.*—/ });
			await provider.waitFor();
			if (await provider.getAttribute('aria-expanded') === 'false') await provider.click();
			for (const [, label] of quotas) {
				const row = panel.locator('.popover-row').filter({ has: page.getByText(label, { exact: true }) });
				await row.getByText('75% left', { exact: true }).waitFor();
				await row.getByText(/^resets in /).waitFor();
				expect(await row.evaluate((el) => el.scrollWidth <= el.clientWidth), `${label} fits its meter row`);
			}
		}
	}],
	allow: DEV_INSTANCE_NOISE
});
