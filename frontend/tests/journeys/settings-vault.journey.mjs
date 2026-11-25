// Settings > Vault on a phone: a sign-in added there is shown in the step its
// form had, so Back returns to the Vault list and then to Settings, instead of
// an empty detail page with no way back.
//
// Every Vault route refuses the preview grant by design, so this journey runs
// as the disposable dev instance's loopback owner (as settings-learned-behaviour
// does). Adding a sign-in contacts nothing and grants nothing.
import { runJourney } from './runner.mjs';
import { expect, gotoApp, openRailPlace, settleServiceWorker } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const mobileShell = process.argv.includes('--mobile-shell');
// One name per run: cleanup removes that sign-in and nothing else on the instance.
let label = '';
// Never used to sign in anywhere: the address does not resolve and the entry is removed afterwards.
const PLACEHOLDER = 'journey-only-placeholder';

await runJourney({
	name: 'settings-vault',
	allow: [
		...DEV_INSTANCE_NOISE,
		{ kind: 'failed_requests', status: 403, url: /\/api\/v1\/terminals\/targets(\?|$)/,
			reason: 'terminal discovery requires more than the passwordless loopback owner used by this dev instance' },
		{ kind: 'failed_requests', status: 401, url: /\/api\/v1\/projects\/[^/]+\/github\/pulls(\?|$)/,
			reason: 'the dev instance has no GitHub sign-in' }
	],
	cases: [{
		name: mobileShell ? 'native-add-then-back' : 'add-then-back',
		viewports: ['phone'],
		async setup({ page, status, deferCleanup }) {
			const port = status.app?.local_port;
			expect(Number.isInteger(port) && port > 1024 && ![8000, 8001, 8002].includes(port),
				'start a disposable dev instance: this journey writes to its Vault');
			const ui = `http://localhost:${port}/`;
			const auth = await page.request.get(new URL('/api/v1/auth/status', ui).href, { maxRedirects: 0 });
			expect(auth.ok(), `disposable loopback owner auth check returned ${auth.status()}`);
			const { authenticated, is_admin, preview_access } = await auth.json();
			expect(authenticated === true && is_admin === true && preview_access === false,
				'loopback must be an authenticated owner, not a preview grant');
			// From this process, not the page: cleanup runs after the page has closed.
			// A loopback caller is the owner here, exactly as the page is.
			const entries = new URL('/api/v1/vault/entries', ui).href;
			const removeOurs = async () => {
				const listed = await fetch(entries);
				if (!listed.ok) throw new Error(`listing the Vault returned ${listed.status}`);
				for (const entry of (await listed.json()).entries.filter((saved) => saved.label === label)) {
					const removed = await fetch(`${entries}/${entry.id}`,
						{ method: 'DELETE', headers: { Origin: new URL(ui).origin, 'Sec-Fetch-Site': 'same-origin' } });
					if (!removed.ok) throw new Error(`removing the journey's sign-in returned ${removed.status}`);
				}
			};
			label = `Journey box ${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
			deferCleanup(removeOurs);
			page.setDefaultTimeout(15_000);
			await gotoApp(page, { urls: { ui } }, mobileShell ? '/mobile/' : '/');
			if (mobileShell) expect(new URL(page.url()).pathname === '/mobile/', 'real mobile bundle');
			await settleServiceWorker(page, { urls: { ui } });
		},
		async act({ page, mark }) {
			const button = (name) => page.getByRole('button', { name, exact: true }).filter({ visible: true });
			const field = (name) => page.getByRole('textbox', { name, exact: true }).filter({ visible: true });
			const row = page.getByRole('button', { name: new RegExp(`^${label}`) }).filter({ visible: true }).first();
			const emptyDetail = page.getByText('Select a sign-in, or add one.').filter({ visible: true });
			const bar = page.locator('.topbar');
			const synced = page.getByText(/Encrypted, and synced to your other workers\./).filter({ visible: true });

			await mark('open Settings > Vault');
			const nav = page.getByRole('navigation', { name: 'Workspace', exact: true });
			if (!await nav.isVisible()) await page.getByRole('button', { name: /^(Open menu|Chats)( \(|$)/ }).first().click();
			await openRailPlace(page, 'Settings');
			await page.getByRole('navigation', { name: 'Settings', exact: true })
				.getByRole('button', { name: /^Vault\b/ }).filter({ visible: true }).first().click();
			await button('Add sign-in').first().waitFor();

			await mark('start an SSH sign-in');
			await button('Add sign-in').first().click();
			await button('SSH machine').click();
			await field('Name').fill(label);
			await field('Address').fill('journey-box.invalid');
			await field('Username').fill('ci');
			await page.getByLabel('Password', { exact: true }).filter({ visible: true }).fill(PLACEHOLDER);

			await mark('add it');
			// A phone keeps a Settings form's actions under More.
			const more = page.getByLabel('More', { exact: true }).filter({ visible: true }).first();
			if (await more.isVisible()) await more.click();
			await page.locator('.detail-actions').getByRole('button', { name: 'Add sign-in', exact: true }).click();
			// The saved sign-in replaces the form: it can now be removed, and its password is not shown.
			await button('Remove').waitFor();
			expect(await page.getByLabel('Password', { exact: true }).filter({ visible: true }).inputValue() === '',
				'the saved password is not sent back to the page');
			expect(await synced.count() === 1, 'the saved sign-in says it syncs to the other workers');

			await mark('Back returns to the Vault list');
			await bar.getByRole('button', { name: 'Back to Vault', exact: true }).click();
			await row.waitFor();
			expect(await emptyDetail.count() === 0, 'Back shows the list, not an empty detail page');
			expect(await synced.count() === 1, 'the list says its sign-ins sync to the other workers');

			await mark('open the sign-in and go back with the system Back');
			await row.click();
			await button('Remove').waitFor();
			await page.goBack();
			await row.waitFor();
			expect(await emptyDetail.count() === 0, 'the system Back shows the list too');

			await mark('Back again reaches the Settings index');
			await bar.getByRole('button', { name: 'Back to Settings', exact: true }).click();
			await page.getByRole('navigation', { name: 'Settings', exact: true }).waitFor();
			expect(await bar.getByRole('button', { name: /^(Chats|Open menu)/ }).count() === 1, 'the menu is one Back away at the index');
		}
	}]
});
