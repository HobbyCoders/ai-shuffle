// Journey: shared controls a phone is tapped with answer across a finger's
// 44px, while a desktop keeps drawing them at its own density (#1528).
//
// Owner report this guards: on a phone, Settings > Providers' Add account and
// Replace, the drawer's rail and the Context and usage hide buttons were
// smaller than a finger, so a tap beside them did nothing.
//
// A control "answers" a point when the element under it is the control or a
// child of it. Each control is probed 21px above, below, left and right of
// its centre: a pixel inside the edge two neighbouring 44px targets share. Saved accounts and one usage row are seeded in window.fetch
// (as provider-account-switch does), so the dev instance's own providers
// never decide the result.
//
//   node tests/journeys/touch-targets.journey.mjs [--only=drawer-rail@phone] [--list]
import { runJourney } from './runner.mjs';
import { expect, gotoApp, isPhone, seededChatId, showSidebar, waitChatOpen } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const ACCOUNT = {
	id: 'journey-touch-account', name: 'Journey touch key', enabled: true,
	provider_type: 'openrouter', slot: 'openrouter:key',
	base_url: 'https://fixture.invalid', auth_token_set: true,
	auth_token_masked: 'fixture-only', models: [], default_model: null,
	small_fast_model: null, extra_env: {}, context_window: null,
	max_output_tokens: null, created_at: null, updated_at: null
};
const USAGE_LINK = {
	provider_id: 'journey-touch-usage', provider_name: 'Journey usage key', provider_type: 'openrouter',
	kind: 'api_key', note: 'Usage is on the vendor dashboard.', dashboard_url: 'https://fixture.invalid'
};

async function seed(page) {
	await page.addInitScript(({ account, link }) => {
		const original = window.fetch.bind(window);
		const json = (body) => new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
		window.fetch = async (input, init) => {
			const path = new URL(typeof input === 'string' ? input : input.url, location.href).pathname;
			const method = (init?.method || input?.method || 'GET').toUpperCase();
			const response = await original(input, init);
			if (method !== 'GET' || !response.ok) return response;
			if (path === '/api/v1/model-providers') {
				const body = await response.json();
				return json({ ...body, providers: [...body.providers, account] });
			}
			if (path === '/api/v1/usage/limits') {
				const body = await response.json();
				return json({ ...body, usage_links: [...(body.usage_links || []), link] });
			}
			return response;
		};
	}, { account: ACCOUNT, link: USAGE_LINK });
}

/**
 * Where a finger beside the control misses it, and the control's drawn size.
 * Probed at the control's centre ± 21px on each axis.
 */
function probe(locator) {
	return locator.evaluate(async (el) => {
		el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
		await new Promise(requestAnimationFrame);
		const box = el.getBoundingClientRect();
		const cx = box.left + box.width / 2;
		const cy = box.top + box.height / 2;
		const misses = [];
		for (const [dx, dy] of [[0, -21], [0, 21], [-21, 0], [21, 0]]) {
			const hit = document.elementFromPoint(cx + dx, cy + dy);
			if (hit && el.contains(hit)) continue;
			misses.push(`(${dx},${dy}) ${hit ? `${hit.tagName.toLowerCase()}.${[...hit.classList].join('.')}` : 'nothing'}`);
		}
		return { misses, width: Math.round(box.width), height: Math.round(box.height) };
	});
}

/** Phone: every control answers a finger. Desktop: each keeps its drawn size. */
async function check(ctx, name, locator, desktop) {
	const { width, height, misses } = await probe(locator);
	ctx.log(`${name}: drawn ${width}x${height}, misses ${misses.length ? misses.join('; ') : 'none'}`);
	if (isPhone(ctx.variant)) expect(!misses.length, `${name} answers a finger's 44px (missed: ${misses.join('; ')})`);
	else if (desktop) expect(desktop(width, height), `${name} keeps its desktop size (drawn ${width}x${height})`);
}

async function openSettings(page) {
	const sidebar = await showSidebar(page);
	await sidebar.getByRole('navigation', { name: 'Workspace' }).getByRole('button', { name: 'Settings', exact: true }).click();
	const settings = page.getByRole('navigation', { name: 'Settings', exact: true });
	await settings.waitFor();
	return settings;
}

await runJourney({
	name: 'touch-targets',
	throttles: ['fast'],
	cases: [
		{
			// The drawer's rail: 36px icons on a desktop, a finger apart on a phone.
			name: 'drawer-rail',
			async setup(ctx) {
				await gotoApp(ctx.page, ctx.status);
			},
			async act(ctx) {
				await ctx.mark('probe each rail button');
				const rail = (await showSidebar(ctx.page)).getByRole('navigation', { name: 'Workspace' });
				const buttons = await rail.getByRole('button').filter({ visible: true }).all();
				expect(buttons.length >= 3, `the rail shows its buttons (${buttons.length})`);
				for (const button of buttons) {
					const label = (await button.getAttribute('aria-label')) || (await button.innerText()).trim();
					await check(ctx, `rail "${label}"`, button, (w, h) => w === 36 && h === 36);
				}
			}
		},
		{
			// Settings > Providers: Add account on the list, Replace on an account.
			name: 'provider-buttons',
			async setup(ctx) {
				await seed(ctx.page);
				await gotoApp(ctx.page, ctx.status);
				await (await openSettings(ctx.page)).getByRole('button', { name: 'Providers', exact: true }).click();
				await ctx.page.locator(`[data-settings-item="mp:${ACCOUNT.id}"]`).waitFor({ timeout: 20_000 });
			},
			async act(ctx) {
				const { page } = ctx;
				await ctx.mark('probe Add account');
				const add = page.getByRole('button', { name: /^Add account to / }).filter({ visible: true });
				expect(await add.count() > 0, 'a provider offers Add account');
				await check(ctx, 'Add account', add.first(), (w, h) => h === 32);
				await ctx.mark('open the saved account and probe Replace');
				await page.locator(`[data-settings-item="mp:${ACCOUNT.id}"]`).click();
				const replace = page.getByRole('button', { name: 'Replace', exact: true }).filter({ visible: true }).first();
				await replace.waitFor({ timeout: 10_000 });
				await check(ctx, 'Replace', replace, (w, h) => h === 32);
			}
		},
		{
			// Context and usage: the hide button beside a usage section's header.
			name: 'usage-hide',
			async setup(ctx) {
				await seed(ctx.page);
				await gotoApp(ctx.page, ctx.status, `/?session=${encodeURIComponent(seededChatId(ctx.status, 'no_project'))}`);
				await waitChatOpen(ctx.page, { messagesAtLeast: 2, hold: 800 });
			},
			async act(ctx) {
				const { page } = ctx;
				await ctx.mark('open Context and usage');
				const controls = page.getByRole('button', { name: /^All chat controls/ });
				if (await controls.isVisible()) {
					await controls.click();
					await page.getByRole('dialog', { name: 'Chat controls' }).getByRole('button', { name: /^Context and usage: / }).click();
				} else {
					await page.getByRole('button', { name: /^Context and usage — / }).first().click();
				}
				const hide = page.getByRole('button', { name: /^Hide .+ usage$/ }).filter({ visible: true }).first();
				await hide.waitFor({ timeout: 15_000 });
				// A phone's sheet slides up first.
				await page.waitForTimeout(600);
				await ctx.mark('probe the hide button');
				await check(ctx, 'Hide usage', hide, (w, h) => w <= 24 && h <= 24);
			}
		}
	],
	allow: DEV_INSTANCE_NOISE
});
