// Run with devloop up --fake-antigravity. The seeded fake CLI returns raw
// /usage JSON; the app's real poller and endpoint must carry it to the UI.
import { runJourney } from './runner.mjs';
import { expect, gotoApp, isPhone, seededChatId, showSidebar, waitChatOpen } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const quotas = [
	['Gemini Models, weekly', '80% left'],
	['Gemini Models, 5-hour', '80% left'],
	['Claude and GPT models, weekly', '75% left'],
	['Claude and GPT models, 5-hour', '75% left'],
	['Gemini 4 Argon, weekly', '0% left']
];

async function accessibleDescription(page, name) {
	const cdp = await page.context().newCDPSession(page);
	try {
		const { nodes } = await cdp.send('Accessibility.getFullAXTree');
		const button = nodes.find((n) => n.role?.value === 'button' && n.name?.value === name);
		return button?.description?.value ?? '';
	} finally {
		await cdp.detach();
	}
}

await runJourney({
	name: 'antigravity-usage',
	recorder: { selectors: ['usage=.usage-popover', 'meters=.popover-row', 'settingsQuota=#providers-antigravity-usage'] },
	cases: [{
		name: 'five-quotas',
		async setup({ page, status }) {
			expect(status.fake_antigravity === true,
				'Antigravity usage fixture is missing: start the dev instance with --fake-antigravity');
			await gotoApp(page, status, `/?session=${encodeURIComponent(seededChatId(status, 'no_project'))}`);
			await waitChatOpen(page, { messagesAtLeast: 2 });
		},
		async act({ page, variant, mark }) {
			await mark('request the real usage poll and wait for its persisted result');
			const reading = await page.evaluate(async () => {
				const read = async (query = '') => {
					const response = await fetch(`/api/v1/usage/limits${query}`);
					if (!response.ok) throw new Error(`Usage API returned ${response.status}`);
					return (await response.json()).providers.find((p) => p.provider_id === 'dev-antigravity-usage');
				};
				let row = await read('?refresh=true');
				// The app's passive opening read may already have used the shared 30 s cooldown.
				for (let i = 0; i < 38 && !row?.last_attempt_at; i++) {
					await new Promise((resolve) => setTimeout(resolve, 1000));
					row = await read('?refresh=true');
				}
				return row;
			});
			await mark(`real usage endpoint: ${reading?.status ?? 'no fixture row'}, ${reading?.windows?.length ?? 0} windows`);
			expect(reading?.status === 'fresh', `expected a fresh CLI reading, got ${reading?.status ?? 'no fixture row'}`);
			expect(JSON.stringify(reading.windows.map((w) => w.label)) === JSON.stringify(quotas.map(([label]) => label)),
				'the real API preserves each grouped quota label in agent order');
			await mark('open the usage meters');
			const controls = page.getByRole('button', { name: 'All chat controls' });
			if (isPhone(variant)) {
				await controls.click();
				await page.getByRole('button', { name: /^Context and usage:/ }).click();
			} else {
				await page.getByRole('button', { name: /^Context and usage —/ }).click();
			}
			const panel = page.locator('.usage-popover');
			// The Subscriptions group starts collapsed on fresh preferences.
			const subscriptions = panel.locator('.usage-group[data-group="group:subscriptions"] > .group-header');
			await subscriptions.waitFor({ timeout: 8000 });
			if (await subscriptions.getAttribute('aria-expanded') === 'false') await subscriptions.click();
			const provider = panel.getByRole('button', { name: /^Antigravity.*usage/ });
			await provider.waitFor({ timeout: 8000 });
			if (await provider.getAttribute('aria-expanded') === 'false') await provider.click();
			const first = panel.getByText(quotas[0][0], { exact: true });
			await first.or(panel.getByText(/^Usage unavailable/)).waitFor({ timeout: 8000 });
			expect(await first.isVisible(), 'the real usage poll supplies the grouped quota windows, not Usage unavailable');
			for (const [label, percentage] of quotas) {
				const row = panel.locator('.popover-row').filter({ has: page.getByText(label, { exact: true }) });
				await row.getByText(percentage, { exact: true }).waitFor();
				await row.getByText(/^resets in /).waitFor();
				expect(await row.evaluate((el) => el.scrollWidth <= el.clientWidth), `${label} fits its meter row`);
			}
			const labels = (await panel.locator('.popover-row .row-label').allTextContents())
				.filter((label) => quotas.some(([quota]) => label === quota));
			expect(JSON.stringify(labels) === JSON.stringify(quotas.map(([label]) => label)), 'agent group order is preserved');
			await mark('collapse Antigravity to its Gemini Models weekly percentage');
			await provider.click();
			await provider.getByText('80% left', { exact: true }).waitFor();
			expect((await provider.locator('.sec-summary').textContent())?.trim() === '80% left',
				'the collapsed Antigravity heading shows Gemini Models weekly, not Argon or Claude/GPT');
			const collapsedDesc = await provider.evaluate((el) => {
				const id = el.getAttribute('aria-describedby');
				const target = id ? document.getElementById(id) : null;
				return {
					id,
					srOnly: target?.classList.contains('sr-only') ?? false,
					text: target?.textContent?.trim() ?? null,
				};
			});
			expect(Boolean(collapsedDesc.id) && collapsedDesc.srOnly && collapsedDesc.text === 'Gemini Models, weekly: 80% left',
				'collapsed Antigravity button links aria-describedby to its .sr-only Gemini weekly summary');
			const collapsedLabel = await provider.getAttribute('aria-label');
			expect(await accessibleDescription(page, collapsedLabel) === 'Gemini Models, weekly: 80% left',
				'collapsed Antigravity button exposes Gemini Models weekly as its accessible description');

			await mark('re-expand Antigravity and clear its collapsed description');
			await provider.click();
			await first.waitFor();
			expect((await provider.getAttribute('aria-expanded')) === 'true', 're-expanded Antigravity section');
			expect((await provider.getAttribute('aria-describedby')) === null,
				're-expanded Antigravity button clears aria-describedby');
			expect(await page.evaluate((id) => document.getElementById(id) === null, collapsedDesc.id),
				're-expanded Antigravity section removes the collapsed .sr-only summary span');
			const expandedLabel = await provider.getAttribute('aria-label');
			expect(await accessibleDescription(page, expandedLabel) === '',
				're-expanded Antigravity button clears its accessible description');

			await mark('open Settings Usage and verify the same reading');
			if (isPhone(variant)) {
				await page.getByRole('button', { name: 'Close chat controls', exact: true }).click();
			} else {
				await page.getByRole('button', { name: /^Context and usage —/ }).click();
			}
			if (isPhone(variant)) await showSidebar(page);
			await page.getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('button', { name: 'Settings', exact: true }).click();
			await page.getByRole('navigation', { name: 'Settings', exact: true }).getByRole('button', { name: 'Providers', exact: true }).click();
			await page.getByRole('button', { name: /^Fixture Antigravity,/ }).click();
			await page.getByRole('tab', { name: 'Usage', exact: true }).click();
			const settings = page.locator('#providers-antigravity-usage');
			for (const [label, percentage] of quotas) {
				await settings.locator('.st-row').filter({ hasText: label }).getByText(percentage, { exact: true }).waitFor();
			}
		},
		async verify({ page, status }) {
			// Keep the simulated time jump out of the flight recorder's timeline.
			const response = await page.request.get(new URL('/api/v1/usage/limits', status.urls.ui).href);
			const { providers } = await response.json();
			const reading = providers.find((p) => p.provider_id === 'dev-antigravity-usage');
			const argon = page.locator('#providers-antigravity-usage .st-row').filter({ hasText: quotas.at(-1)[0] });
			await page.clock.setFixedTime(Date.parse(reading.windows.at(-1).resets_at) + 1000);
			await argon.getByText('100% left', { exact: true }).waitFor({ timeout: 5000 });
			await argon.getByText('Reset · awaiting next report', { exact: true }).waitFor();
		}
	}],
	allow: DEV_INSTANCE_NOISE
});
