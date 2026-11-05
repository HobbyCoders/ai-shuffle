// Regression: rail-owned overlays must not live inside the hidden/inert Chats column.
import { runJourney } from './runner.mjs';
import { expect, gotoApp } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const workspace = page => page.getByRole('navigation', { name: 'Workspace', exact: true });
const menu = page => page.getByRole('menu', { name: 'More destinations', exact: true });
async function openMore(page, native) {
	if (native) {
		await page.getByRole('button', { name: 'Open menu', exact: true }).click();
		await workspace(page).waitFor();
	}
	await workspace(page).getByRole('button', { name: 'More', exact: true }).click();
	await menu(page).waitFor({ timeout: 5000 });
}

await runJourney({
	name: 'rail-more',
	allow: [
		...DEV_INSTANCE_NOISE,
		{
			kind: 'failed_requests', status: 401, url: /\/github\/inbox(\?|$)/,
			reason: 'the dev instance has no GitHub sign-in, so the Pull requests inbox cannot load'
		}
	],
	cases: [false, true].map(native => ({
		name: native ? 'native-drawer-after-feature' : 'docked-after-feature',
		viewports: [native ? 'phone' : 'desktop'],
		async setup({ page, status }) {
			if (native) {
				await page.goto(new URL('/mobile/', status.urls.preview_listener).href);
				await openMore(page, true);
				await menu(page).getByRole('menuitem', { name: 'Pull requests', exact: true }).click();
			} else {
				await gotoApp(page, status);
				await workspace(page).getByRole('button', { name: 'Pull requests', exact: true }).click();
				await page.locator('.page-panel h1').waitFor();
			}
		},
		async act({ page, mark }) {
			for (const label of ['Docs', 'Stats']) {
				await mark(`open More from a feature and navigate to ${label}`);
				await openMore(page, native);
				const item = menu(page).getByRole('menuitem', { name: label, exact: true });
				expect(await item.evaluate(el => !el.closest('[inert], [aria-hidden="true"]')), 'rail menu is outside hidden Chats');
				await item.click();
				await menu(page).waitFor({ state: 'hidden' });
			}
			await openMore(page, native);
			expect(await menu(page).getByRole('menuitem', { name: 'Stats', exact: true }).getAttribute('aria-current') === 'page', 'Stats is the active destination');
			await mark('open and close Customize from the active feature');
			await menu(page).getByRole('menuitem', { name: /Customize the rail/ }).click();
			const dialog = page.getByRole('dialog', { name: 'Customize the rail', exact: true });
			await dialog.waitFor();
			await dialog.getByRole('button', { name: 'Done', exact: true }).click();
			await workspace(page).getByRole('button', { name: 'More', exact: true }).click();
			await menu(page).waitFor();
			await mark('Escape closes the active navigation overlay and returns focus to its launcher');
			await page.keyboard.press('Escape');
			await menu(page).waitFor({ state: 'hidden' });
			// The native shell owns Escape for the entire drawer; desktop closes just More.
			const launcher = native ? page.getByRole('button', { name: 'Open menu', exact: true }) : workspace(page).getByRole('button', { name: 'More', exact: true });
			expect(await launcher.evaluate(el => el === document.activeElement), 'focus returns to the navigation launcher');
		}
	}))
});
