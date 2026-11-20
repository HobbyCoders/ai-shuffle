// The real Settings UI with a test-only preload stand-in. Never writes OS settings.
import { runJourney } from './runner.mjs';
import { expect, gotoApp, showSidebar } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const name = 'Start AI Shuffle at sign-in';
const variants = { viewports: ['desktop', 'phone'], throttles: ['fast'] };
const startup = (page) => page.getByRole('switch', { name });
const openSettings = async (page, variant) => {
	if (variant.viewport === 'phone') await showSidebar(page);
	await page.getByRole('navigation', { name: 'Workspace', exact: true })
		.getByRole('button', { name: 'Settings', exact: true }).click();
};
const search = (page) => page.getByRole('combobox', { name: 'Search settings' });
const result = (page) => page.getByRole('option', { name: new RegExp(name) });

await runJourney({ name: 'desktop-startup', allow: DEV_INSTANCE_NOISE, cases: [
	{
		name: 'uses-os-readback', ...variants,
		async setup({ page, status, variant }) {
			await page.addInitScript(() => {
				let state = 'disabled';
				const noSubscription = () => () => {};
				window.shuffleDesktop = {
					platform: 'win32', version: 'test',
					window: { getState: async () => ({ focused: true }), onStateChange: noSubscription, setTitleBarTheme() {} },
					shell: { onCommand: noSubscription },
					updates: { getState: async () => ({ phase: 'idle' }), onState: noSubscription },
					loginItem: {
						get: async () => state,
						set: async (on) => ({ state: state = on ? 'enabled' : 'disabled', failed: false })
					}
				};
			});
			await gotoApp(page, status);
			await openSettings(page, variant);
		},
		async act({ page, mark }) {
			await mark('Search Settings for startup');
			await search(page).fill('startup');
			await result(page).click();
			const control = startup(page);
			await control.and(page.locator('[aria-checked="false"]:enabled')).waitFor();
			await mark('Opt in via the keyboard');
			await control.press('Space');
			await control.and(page.locator('[aria-checked="true"]:enabled')).waitFor();
			await mark('Turn off startup');
			await control.click();
			await control.and(page.locator('[aria-checked="false"]:enabled')).waitFor();
		}
	},
	{
		name: 'omits-startup-on-web', ...variants,
		async setup({ page, status, variant }) {
			await gotoApp(page, status);
			await openSettings(page, variant);
		},
		async act({ page, mark }) {
			await mark('Open Settings > General in a browser');
			await page.getByRole('navigation', { name: 'Settings', exact: true })
				.getByRole('button', { name: 'General', exact: true }).first().click();
			await page.getByRole('heading', { name: 'Appearance', exact: true }).waitFor();
			expect(await startup(page).count() === 0, 'web has no startup toggle');
		}
	}
] });
