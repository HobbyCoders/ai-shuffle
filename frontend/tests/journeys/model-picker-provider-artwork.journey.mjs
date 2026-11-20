// Real picker DOM with a controlled registry; no provider credentials or calls.
import { runJourney } from './runner.mjs';
import { expect, gotoApp, openChatSetup, openPopup, showSidebar } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const opusProviders = [['Antigravity', 'antigravity'], ['Claude', 'claude'], ['Private Gateway', 'custom']];
const longModel = 'Opus ' + 'long-horizontal-model-name-'.repeat(10);
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
				{ id: 'artwork-custom', name: 'Private Gateway', provider_type: 'custom', auth_kind: 'api', models: [{ id: 'claude-opus', label: 'Opus' }, { id: 'long', label: longModel }] },
				{ id: 'artwork-backup', name: 'Backup Gateway', provider_type: 'custom', auth_kind: 'api', models: [{ id: 'long', label: longModel }] }
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

const longDuplicate = {
	name: 'long-duplicate', viewports: ['phone'], throttles: ['fast'], setup: settings.setup,
	async act({ page, mark, log }) {
		await mark('check two long duplicate names with the same fallback mark');
		await openPopup(page.locator('#api-user-model'), picker(page));
		await picker(page).getByRole('textbox', { name: 'Search models' }).fill('long-horizontal');
		const rows = picker(page).getByRole('option', { name: longModel, exact: true });
		expect(await rows.count() === 2, 'both gateway models are present');
		for (const gateway of ['Private Gateway', 'Backup Gateway']) {
			const row = rows.filter({ has: page.locator('.model-provider', { hasText: gateway }) });
			const visibleText = await row.locator('.model-provider').evaluate((el) => {
				const clip = el.closest('.mpp-row-copy').getBoundingClientRect();
				const range = document.createRange();
				range.selectNodeContents(el);
				return [...range.getClientRects()].every((r) => r.width > 0 && r.left >= clip.left - 1 && r.right <= clip.right + 1);
			});
			expect(visibleText, `${gateway} remains legible without relying on the glyph`);
		}
		log('Both gateway labels fit beside the long duplicate names at phone width.');
	}
};

// All writes here are fulfilled by Playwright; no funding configuration is touched.
const voiceWrites = new WeakMap();
const voiceForm = {
	name: 'voice-form-selection', throttles: ['fast'],
	async setup(ctx) {
		const { page } = ctx;
		const writes = [];
		voiceWrites.set(page, writes);
		let voice;
		await page.route('**/api/v1/settings/voice', async (route) => {
			if (route.request().method() === 'GET') {
				if (!voice) voice = {
					...await (await route.fetch()).json(), voice_model: 'gpt-live-1', voice_provider_id: 'fixture-openai',
					voice_engines: [{ id: 'gpt-live-1', label: 'GPT Live 1', funding: 'openai' },
						{ id: 'grok-voice-latest', label: 'Grok Voice', funding: 'xai' }]
				};
			} else {
				const body = route.request().postDataJSON();
				writes.push(body);
				voice = { ...voice, ...body };
			}
			await route.fulfill({ json: voice });
		});
		await page.route('**/api/v1/voice/providers', (route) => route.fulfill({ json: [
			{ id: 'fixture-openai', name: 'Fixture OpenAI', provider_type: 'openai' },
			{ id: 'fixture-xai', name: 'Fixture xAI', provider_type: 'xai' }
		] }));
		await load(ctx);
		const nav = page.getByRole('navigation', { name: 'Workspace', exact: true });
		if (!await nav.isVisible()) await showSidebar(page);
		await nav.getByRole('button', { name: 'Settings', exact: true }).click();
		await page.getByRole('navigation', { name: 'Settings', exact: true }).getByRole('button', { name: 'Voice', exact: true }).click();
	},
	async act({ page, mark, log }) {
		const trigger = page.getByRole('button', { name: 'Voice engine', exact: true });
		await mark('reselect the engine with a pinned provider');
		await openPopup(trigger, picker(page));
		await picker(page).getByRole('option', { name: 'GPT Live 1', exact: true }).click();
		expect(await trigger.evaluate((el) => el === document.activeElement), 'reselection restores focus');
		expect(!await picker(page).isVisible(), 'reselection closes the form picker');
		await mark('choose a different voice engine');
		await openPopup(trigger, picker(page));
		const response = page.waitForResponse((r) => r.url().endsWith('/settings/voice') && r.request().method() === 'PUT');
		await picker(page).getByRole('option', { name: 'Grok Voice', exact: true }).click();
		await response;
		const writes = voiceWrites.get(page);
		expect(writes.length === 1 && writes[0].voice_model === 'grok-voice-latest' && writes[0].voice_provider_id === null,
			'only the real engine change sends a settings update, with the legacy pin reset');
		expect(await trigger.evaluate((el) => el === document.activeElement), 'engine change restores focus');
		log('No write for reselect; one intercepted PUT for Grok Voice cleared the previous pin. No live voice setting changed.');
	}
};

const automationWrites = new WeakMap();
const jobId = '11111111-2222-4333-8444-555555555555';
const imageChoice = {
	source: 'fal', account: 'fixture-fal', worker: 'fixture-worker', model: 'fal-ai/flux/dev', kind: 'image',
	options: {}, verified: true, price: { currency: 'USD', unit: 'item', amount: '0.02', total: '0.02', status: 'estimated' },
	schema: null, available: true, reason: null
};
const automationForm = {
	name: 'automation-form-selection', throttles: ['fast'],
	async setup(ctx) {
		const { page } = ctx;
		const writes = {};
		automationWrites.set(page, writes);
		await page.route('**/api/v1/generation/options/image*', (route) => route.fulfill({ json: { options: [
			{ ...imageChoice, model: 'fal-ai/flux/schnell' },
			{ ...imageChoice, model: 'fal-ai/unavailable', available: false, reason: 'Unavailable in fixture' }, imageChoice
		] } }));
		await page.route('**/api/v1/automations', (route) => {
			if (route.request().method() !== 'POST') return route.continue();
			writes.automation = route.request().postDataJSON();
			return route.fulfill({ json: { ...writes.automation, id: jobId, is_enabled: false, run_count: 0, failure_count: 0, status: 'idle' } });
		});
		await page.route(`**/api/v1/generation/jobs/${jobId}/choices/image`, (route) => {
			writes.image = route.request().postDataJSON();
			return route.fulfill({ json: { job_id: jobId, kind: 'image', choice: writes.image } });
		});
		await automation.setup(ctx);
	},
	async act({ page, mark, log }) {
		await page.getByPlaceholder('e.g. Daily report generator').fill('Artwork fixture automation');
		await page.getByPlaceholder(/Enter the prompt template/).fill('Nothing will be dispatched.');
		await mark('choose Antigravity Opus instead of Claude Opus');
		await openPopup(page.locator('#automation-model'), picker(page));
		await checkRows(page);
		await picker(page).getByRole('option', { name: 'Opus', exact: true })
			.filter({ has: page.locator('.model-provider', { hasText: /Antigravity/i }) }).click();
		await page.getByRole('tab', { name: 'Media generation' }).click();
		await page.locator('.ajc-row[data-kind="image"]').getByRole('button', { name: 'Pin a choice' }).click();
		const trigger = page.getByRole('button', { name: 'Source and model' });
		await mark('click an unavailable option then recover with the keyboard');
		await openPopup(trigger, picker(page));
		const search = picker(page).getByRole('textbox', { name: 'Search models' });
		await search.focus(); // On touch-first viewports search does not autofocus by design.
		const unavailable = picker(page).getByRole('option', { name: /unavailable/i });
		await unavailable.scrollIntoViewIfNeeded();
		const bounds = await unavailable.boundingBox();
		await page.mouse.click(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
		expect(await search.evaluate((el) => el === document.activeElement), 'disabled row does not steal search focus');
		await search.fill('flux dev');
		await page.keyboard.press('ArrowDown');
		await page.keyboard.press('Enter');
		expect(await trigger.evaluate((el) => el === document.activeElement), 'keyboard selection returns focus');
		await mark('stage and capture the job payloads');
		await page.getByRole('button', { name: 'Pin to this job' }).click();
		const response = page.waitForResponse((r) => r.url().endsWith(`/generation/jobs/${jobId}/choices/image`) && r.request().method() === 'PUT');
		await page.getByRole('button', { name: 'Create', exact: true }).click();
		await response;
		const { automation, image } = automationWrites.get(page);
		expect(automation.model === 'prov:artwork-ag:claude-opus', 'the model payload retains the chosen Opus provider');
		expect(image.source === 'fal' && image.account === imageChoice.account && image.worker === imageChoice.worker && image.model === imageChoice.model,
			'the staged media selection retains its exact provider/account/worker/model');
		log('Intercepted, not sent: automation create used Antigravity Opus, and the image PUT used the keyboard-selected fixture route. No job created or run.');
	}
};

await runJourney({
	name: 'model-picker-provider-artwork',
	allow: DEV_INSTANCE_NOISE,
	cases: [composerCase(), settings, automation, longDuplicate, voiceForm, automationForm, ...(process.argv.includes('--mobile-shell') ? [composerCase(true)] : [])]
});
