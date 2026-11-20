// Real picker DOM with a controlled registry; no provider credentials or calls.
import { runJourney } from './runner.mjs';
import { expect, gotoApp, openChatSetup, openPopup, showSidebar } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const opusProviders = [['Antigravity', 'antigravity'], ['Claude', 'claude'], ['Private Gateway', 'custom']];
const picker = (page) => page.locator('[data-model-picker-panel]').filter({ visible: true });

async function load({ page, status }, native = false) {
	await page.route('**/api/v1/models', async (route) => {
		const response = await route.fetch();
		const data = await response.json();
		await route.fulfill({ response, json: {
			...data,
			models: data.models.map((model) => model.value === 'opus' ? { ...model, label: 'Opus' } : model),
			claude: { auth_kind: 'subscription', plan_type: 'pro' },
			providers: [
				{ id: 'artwork-ag', name: 'Antigravity', provider_type: 'antigravity', auth_kind: 'subscription', usable: true, models: [{ id: 'claude-opus', label: 'Opus' }] },
				{ id: 'artwork-custom', name: 'Private Gateway', provider_type: 'custom', auth_kind: 'api', models: [{ id: 'claude-opus', label: 'Opus' }] }
			]
		} });
	});
	if (native) {
		await page.goto(new URL('/mobile/', status.urls.preview_listener).href);
		expect(new URL(page.url()).pathname === '/mobile/', 'native evidence uses the real phone bundle');
		await page.getByRole('button', { name: /^Model: / }).first().waitFor();
	} else await gotoApp(page, status);
}

async function checkRows(page, providers = opusProviders) {
	for (const [provider, glyph] of providers) {
		const row = picker(page).getByRole('option', { name: 'Opus', exact: true })
			.filter({ has: page.locator('.model-provider').filter({ hasText: new RegExp(provider, 'i') }) });
		expect(await row.count() === 1, `Opus through ${provider} must be named on its own row`);
		const described = await row.evaluate((el) => el.querySelector('.model-provider')?.id === el.getAttribute('aria-describedby'));
		expect(described, `${provider} is exposed as the row's accessible description`);
		const art = row.locator(`[data-provider-logo="${glyph}"]`);
		expect(await art.isVisible(), `${provider} row renders ${glyph} artwork`);
		if (glyph === 'antigravity') await art.evaluate((el) => el.decode());
	}
}

function composerCase(native = false) {
	return {
		name: native ? 'native-duplicate-models' : 'duplicate-models',
		...(native ? { viewports: ['phone'] } : {}),
		setup: (ctx) => load(ctx, native),
		async act({ page, mark, log }) {
			await mark('open the composer model picker');
			await openChatSetup(page);
			const controls = page.getByRole('dialog', { name: 'Chat controls' });
			const host = await controls.isVisible() ? controls : page;
			if (!(await picker(page).isVisible())) {
				await openPopup(host.getByRole('button', { name: /^Model: / }).filter({ visible: true }).first(), picker(page));
			}
			await checkRows(page);
			await mark('search for the same model on different providers');
			await picker(page).getByRole('textbox', { name: 'Search models' }).fill('opus');
			await checkRows(page);
			log('Grouped and searched Opus rows name the serving provider; Antigravity image decoded, Claude vector and custom fallback visible.');
		}
	};
}

const settings = {
	name: 'settings-api-user',
	throttles: ['fast'],
	async setup(ctx) {
		await load(ctx);
		const { page } = ctx;
		const nav = page.getByRole('navigation', { name: 'Workspace', exact: true });
		if (!(await nav.isVisible())) await showSidebar(page);
		await nav.getByRole('button', { name: 'Settings', exact: true }).click();
		await page.getByRole('navigation', { name: 'Settings', exact: true }).getByRole('button', { name: 'Integrations', exact: true }).click();
		await page.getByRole('button', { name: 'Add API user', exact: true }).first().click();
		await page.getByRole('tab', { name: 'Run', exact: true }).click();
	},
	async act({ page, mark, log }) {
		await mark('open the API-key-only model picker');
		await openPopup(page.locator('#api-user-model'), picker(page));
		await checkRows(page, [['Private Gateway', 'custom']]);
		expect(await picker(page).getByRole('option', { name: 'Opus', exact: true }).count() === 1, 'subscription routes are still excluded');
		log('Settings API-user picker shows the gateway fallback and still excludes subscription Opus rows.');
	}
};

const automation = {
	name: 'automation-reasoning-model',
	throttles: ['fast'],
	async setup(ctx) {
		await load(ctx);
		const { page } = ctx;
		const nav = page.getByRole('navigation', { name: 'Workspace', exact: true });
		if (!(await nav.isVisible())) await showSidebar(page);
		await nav.getByRole('button', { name: 'More', exact: true }).click();
		await page.getByRole('menuitem', { name: 'Automations', exact: true }).click();
		await page.getByRole('button', { name: 'New automation', exact: true }).first().click();
		await page.locator('#automation-model').waitFor();
	},
	async act({ page, mark, log }) {
		await mark('open the automation reasoning-model picker');
		await openPopup(page.locator('#automation-model'), picker(page));
		await picker(page).getByRole('textbox', { name: 'Search models' }).fill('opus');
		await checkRows(page);
		log('Automation reasoning-model search shows all three Opus provider identities without saving or running a job.');
	}
};

await runJourney({
	name: 'model-picker-provider-artwork',
	allow: DEV_INSTANCE_NOISE,
	cases: [composerCase(), settings, automation, ...(process.argv.includes('--mobile-shell') ? [composerCase(true)] : [])]
});
