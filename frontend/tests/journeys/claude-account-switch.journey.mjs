// Drive the actual Settings owner mutation on a disposable fake-CLI worker.
import { mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { runJourney } from './runner.mjs';
import { gotoApp, showSidebar } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const auth = '/api/v1/settings/claude-auth';
// Paired local runs may use the launcher's private listener credential. CI's
// single-worker loopback has no listener token. Never use a preview grant here.
const token = process.env.JOURNEY_DEV_LISTENER_TOKEN;
const listener = token ? { 'X-Shuffle-Desktop-Listener': token } : {};
function own(status) {
	return `http://localhost:${status.app.local_port}`;
}
async function write(page, origin, path, data) {
	const response = await page.request.put(new URL(path, origin).href, {
		data, headers: { Origin: origin, 'Sec-Fetch-Site': 'same-origin' }
	});
	if (!response.ok()) throw new Error(`PUT ${path} returned ${response.status()}`);
}

async function ownerSession(page, origin) {
	if (!token) return; // single-worker devloop already trusts its owner loopback
	const start = await page.request.post(`${origin}/api/v1/owner/handoff`, { headers: listener });
	if (!start.ok()) throw new Error(`Owner handoff returned ${start.status()}`);
	const { path } = await start.json();
	// Redeem via the context's request client so the temporary URL stays off
	// the page and out of the recorder. The cookie covers HTTP and WebSockets.
	const redeem = await page.request.get(new URL(path, origin).href, { maxRedirects: 0 });
	if (redeem.status() !== 303) throw new Error(`Owner handoff redemption returned ${redeem.status()}`);
}

await runJourney({
	name: 'claude-account-switch',
	allow: DEV_INSTANCE_NOISE,
	throttles: ['fast'],
	browser: async () => ({ context: { serviceWorkers: 'block' } }),
	cases: [{
		name: 'recover-pending-subscription',
		async setup({ page, status }) {
			const worker = status.workers?.[0] ?? status;
			if (worker.claude_cli?.mode !== 'fake' || !worker.app.local_port) {
				throw new Error('requires a private devloop with fake Claude and a local owner port');
			}
			const origin = own(worker);
			await ownerSession(page, origin);
			const folder = join(worker.scratch_dir, 'home', '.claude');
			mkdirSync(folder, { recursive: true });
			const marker = join(folder, 'devloop-account.txt');
			try {
				writeFileSync(marker, '');
				await write(page, origin, auth, { mode: 'api_key' });
				await write(page, origin, auth, { mode: 'subscription' });
				const state = await (await page.request.get(`${origin}${auth}?refresh=true`)).json();
				if (state.cli.signed_in) throw new Error('subscription must start unavailable');
				// Fake external `claude auth login`. It changes only the local CLI state.
				writeFileSync(marker, 'fake-claude');
				await gotoApp(page, { ...status, urls: { ui: origin } });
				const sidebar = await showSidebar(page);
				await sidebar.getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('button', { name: 'Settings', exact: true }).click();
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
			const result = await page.request.get(new URL(`${auth}?refresh=true`, page.url()).href);
			const state = await result.json();
			if (!state.cli.signed_in || state.cli.email !== 'fake-claude@devloop.invalid' ||
				!state.gate.claude_credential_ready || state.accounts.recovery_required) {
				throw new Error('status, gate and account list disagree after the explicit selection');
			}
		}
	}]
});
