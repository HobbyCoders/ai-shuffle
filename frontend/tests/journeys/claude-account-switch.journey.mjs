// Drive the actual Settings owner mutation on a disposable fake-CLI worker.
import { mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { runJourney } from './runner.mjs';
import { gotoApp, isPhone, showSidebar } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const auth = '/api/v1/settings/claude-auth';
// Paired local runs may use the launcher's private listener credential. CI's
// single-worker loopback has no listener token. Never use a preview grant here.
const token = process.env.JOURNEY_DEV_LISTENER_TOKEN;
const listener = token ? { 'X-Shuffle-Desktop-Listener': token } : {};
function own(status) {
	return `http://localhost:${status.app.local_port}`;
}
async function write(page, path, method = 'POST', data) {
	const response = await page.request.fetch(new URL(path, page.url()).href, {
		method, data, headers: { ...listener, Origin: new URL(page.url()).origin, 'Sec-Fetch-Site': 'same-origin' }
	});
	if (!response.ok()) throw new Error(`${method} ${path} returned ${response.status()}`);
	return response.json();
}

await runJourney({
	name: 'claude-account-switch',
	allow: DEV_INSTANCE_NOISE,
	throttles: ['fast'],
	browser: async () => ({ context: { serviceWorkers: 'block' } }),
	cases: [{
		name: 'recover-pending-subscription',
		async setup({ page, status, variant }) {
			const worker = status.workers?.[0] ?? status;
			if (worker.claude_cli?.mode !== 'fake' || !worker.app.local_port) {
				throw new Error('requires a private devloop with fake Claude and a local owner port');
			}
			if (token) {
				await page.route(`${own(worker)}/**`, (route) => route.continue({
					headers: { ...route.request().headers(), ...listener }
				}));
			}
			const folder = join(worker.scratch_dir, 'home', '.claude');
			mkdirSync(folder, { recursive: true });
			const marker = join(folder, 'devloop-account.txt');
			try {
				writeFileSync(marker, '');
				await gotoApp(page, { ...status, urls: { ui: own(worker) } });
				await write(page, auth, 'PUT', { mode: 'api_key' });
				await write(page, auth, 'PUT', { mode: 'subscription' });
				const state = await (await page.request.get(new URL(`${auth}?refresh=true`, page.url()).href, { headers: listener })).json();
				if (state.cli.signed_in) throw new Error('subscription must start unavailable');
				// Fake external `claude auth login`. It changes only the local CLI state.
				writeFileSync(marker, 'fake-claude');
				if (isPhone(variant)) await showSidebar(page);
				await page.getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('button', { name: 'Settings', exact: true }).click();
				await page.getByRole('navigation', { name: 'Settings', exact: true }).getByRole('button', { name: 'Providers', exact: true }).click();
				await page.locator('[data-settings-item="provider:claude"]:visible').first().click();
				await page.getByText(/Update all workers before relying on a switch/).waitFor();
				await page.getByRole('button', { name: /Open Claude subscription/i }).click();
				await page.getByRole('button', { name: 'Use this sign-in', exact: true }).waitFor();
			} finally {
				rmSync(marker, { force: true }); // default fake CLI reports the same identity
			}
		},
		async act({ page, mark }) {
			await mark('verify and select the default CLI sign-in after external login');
			const changed = page.waitForResponse((res) => res.url().endsWith(`${auth}/accounts/subscription/use-default`));
			await page.getByRole('button', { name: 'Use this sign-in', exact: true }).click();
			if (!(await changed).ok()) throw new Error('verified default selection failed');
			const result = await page.request.get(new URL(`${auth}?refresh=true`, page.url()).href, { headers: listener });
			const state = await result.json();
			if (!state.cli.signed_in || state.cli.email !== 'fake-claude@devloop.invalid' ||
				!state.gate.claude_credential_ready || state.accounts.recovery_required) {
				throw new Error('status, gate and account list disagree after the explicit selection');
			}
		}
	}]
});
