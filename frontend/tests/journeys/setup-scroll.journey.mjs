// Keep the real wizard open on the CI worker without changing its setup state.
import { expect } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
import { runJourney } from './runner.mjs';

const step = (page, name) => page.getByTestId(`setup-${name}`);
const button = (scope, name) => scope.getByRole('button', { name, exact: true });

async function stage(page, status, gate) {
	const signIn = gate === 'sign-in';
	await page.route('**/api/v1/auth/status', async (route) => {
		const response = await route.fetch();
		await route.fulfill({ response, json: {
			...(await response.json()), onboarding_complete: false, ...(signIn && { authenticated: false })
		} });
	});
	if (gate) {
		await page.route('**/api/v1/owner/status', async (route) => {
			const response = await route.fetch();
			await route.fulfill({ response, json: {
				...(await response.json()), required: !signIn, password_set: signIn, login_available: signIn, desktop_handoff: false
			} });
		});
	}
	await page.goto(new URL('/setup', status.urls.ui).href);
	if (!signIn) await step(page, 'welcome').waitFor();
}

async function measure(target) {
	return target.evaluate((el) => {
		const rect = el.getBoundingClientRect();
		const scroll = [];
		for (let parent = el.parentElement; parent; parent = parent.parentElement) {
			if (/^(auto|scroll)$/.test(getComputedStyle(parent).overflowY) && parent.scrollHeight > parent.clientHeight) {
				scroll.push({ top: parent.scrollTop, range: parent.scrollHeight - parent.clientHeight });
			}
		}
		return { top: rect.top, bottom: rect.bottom, height: visualViewport.height, doc: window.scrollY, scroll };
	});
}

async function reachFooter(page, target, mark, log, { mustScroll = true } = {}) {
	await target.waitFor();
	const before = await measure(target);
	if (mustScroll) expect(before.top >= before.height, `footer starts below the fold: ${JSON.stringify(before)}`);
	await mark('wheel down to step navigation');
	const { width, height } = page.viewportSize();
	await page.mouse.move(width / 2, height / 2);
	// Do not use locator.click/scrollIntoView to prove reachability: Chromium
	// can scroll an overflow:hidden ancestor programmatically even when a user cannot.
	let after;
	for (let i = 0; i < 3; i++) {
		await page.mouse.wheel(0, 1500);
		for (let frame = 0; frame < 8; frame++) {
			await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(resolve)));
			after = await measure(target);
			if (after.top >= 0 && after.bottom <= after.height) break;
		}
		if (after.top >= 0 && after.bottom <= after.height) break;
	}
	log(`footer geometry ${JSON.stringify(before)} -> ${JSON.stringify(after)}`);
	expect(after.top >= 0 && after.bottom <= after.height, `wheel did not bring the footer into view: ${JSON.stringify(after)}`);
	expect(after.doc === 0 && after.scroll.length <= 1, `expected one scroll owner and a fixed document: ${JSON.stringify(after)}`);
	if (mustScroll) expect(after.scroll[0]?.top > 0, 'the setup scroll owner moved');
}

async function click(target, mark, label) {
	await mark(label);
	await target.click();
}

await runJourney({
	name: 'setup-scroll',
	browser: async () => ({ context: { serviceWorkers: 'block' } }),
	viewports: ['desktop'],
	throttles: ['fast'], // Geometry/wheel input; no provider or timing dependency.
	allow: [...DEV_INSTANCE_NOISE],
	cases: [
		{
			name: 'short-wizard',
			async setup({ page, status }) {
				await page.setViewportSize({ width: 390, height: 360 });
				await stage(page, status);
			},
			async act({ page, mark, log }) {
				for (const name of ['welcome', 'license', 'provider']) {
					const target = button(step(page, name), name === 'provider' ? 'Back' : 'Continue');
					await reachFooter(page, target, mark, log, { mustScroll: name === 'welcome' });
					await click(target, mark, `use ${name} navigation`);
				}
				await step(page, 'license').waitFor();
			}
		},
		{
			name: 'short-provider',
			async setup({ page, status }) {
				await page.setViewportSize({ width: 390, height: 360 });
				await stage(page, status);
				// Stage past the earlier steps without relying on their scroll behavior.
				for (const name of ['welcome', 'license']) {
					await button(step(page, name), 'Continue').evaluate((el) => el.click());
				}
				await step(page, 'provider').waitFor();
			},
			async act({ page, mark, log }) {
				const provider = step(page, 'provider');
				const back = button(provider, 'Back');
				await reachFooter(page, back, mark, log);
				await click(back, mark, 'return from AI provider to Cloud');
				await step(page, 'license').waitFor();
				const heading = await measure(page.getByRole('heading', { level: 1 }));
				expect(heading.top >= 0 && heading.bottom <= heading.height, 'returning to a step shows its heading');
			}
		},
		{
			name: 'short-owner-gate',
			async setup({ page, status }) {
				await page.setViewportSize({ width: 390, height: 360 });
				await stage(page, status, 'create');
			},
			async act({ page, mark, log }) {
				const next = button(step(page, 'welcome'), 'Continue');
				await reachFooter(page, next, mark, log);
				await click(next, mark, 'open owner password step');
				const form = page.getByRole('form', { name: 'Set an owner password' });
				await reachFooter(page, button(form, 'Save password'), mark, log);
				await click(button(form, 'Back'), mark, 'return to welcome without saving a password');
				await step(page, 'welcome').waitFor();
			}
		},
		{
			name: 'short-owner-sign-in',
			async setup({ page, status }) {
				await page.setViewportSize({ width: 390, height: 220 });
				// A fresh Docker install with OWNER_PASSWORD set shows the login gate,
				// not the Welcome/password-creation branch. Do not enter credentials.
				await stage(page, status, 'sign-in');
				await page.getByRole('form', { name: 'Sign in to AI Shuffle' }).waitFor();
			},
			async act({ page, mark, log }) {
				const signIn = button(page.getByRole('form', { name: 'Sign in to AI Shuffle' }), 'Sign in');
				await reachFooter(page, signIn, mark, log);
				expect(await signIn.isDisabled(), 'sign-in still requires entering a password');
			}
		},
		{
			name: 'normal-wizard',
			async setup({ page, status }) { await stage(page, status); },
			async act({ page, mark, log }) {
				const next = button(step(page, 'welcome'), 'Continue');
				await reachFooter(page, next, mark, log, { mustScroll: false });
				const state = await measure(next);
				expect(state.scroll.length === 0, 'a fitting step does not scroll');
				const card = await page.getByRole('heading', { level: 1 }).evaluate((el) => {
					const { top, bottom } = el.closest('.ui-onboarding-card').getBoundingClientRect();
					return { top, below: visualViewport.height - bottom };
				});
				expect(Math.abs(card.top - card.below) <= 1, `the fitting card stays centered: ${JSON.stringify(card)}`);
				await click(next, mark, 'continue from the centered welcome card');
				await step(page, 'license').waitFor();
			}
		}
	]
});
