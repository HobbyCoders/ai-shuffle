// Drive the actual Settings owner mutation on a disposable fake-CLI worker.
import { closeSync, mkdirSync, openSync, realpathSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { runJourney } from './runner.mjs';
import { gotoApp, showSidebar } from './lib/app.mjs';
import { startClaudeAuthWorker } from './lib/claude-auth-worker.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const auth = '/api/v1/settings/claude-auth';
function headers(origin) {
	return { Origin: origin, 'Sec-Fetch-Site': 'same-origin' };
}
async function request(page, origin, method, path, data) {
	const response = await page.request.fetch(new URL(path, origin).href, { method, data, headers: headers(origin) });
	if (!response.ok()) throw new Error(`${method} ${path} returned ${response.status()}`);
	return response.json();
}
const statusPath = `${auth}?refresh=true`;
// Exact retries can request several variants, and a whole-file retry can run
// all four. Share the work deadline rather than granting a new budget per case.
// Hosted CI only retries this journey with at least 178s left (journey_retry.py).
// Leave 24.5s for the last fixture (TERM, KILL, guardian) and 8s for browser
// closure and reports. An earlier case's teardown counts against the work window.
const workDeadline = performance.now() + 145_000;

await runJourney({
	name: 'claude-account-switch',
	isolated: true, // The cases provision private workers; never mutate the shard's worker.
	allow: DEV_INSTANCE_NOISE,
	throttles: ['fast'],
	caseTimeoutMs: () => Math.min(42_000, Math.floor(workDeadline - performance.now())),
	browser: async () => ({ timeout: 10_000, context: { serviceWorkers: 'block' } }),
	cases: [false, true].map((existing) => ({
		name: existing ? 'recover-with-extra-sign-in' : 'recover-pending-subscription',
		async setup(ctx) {
			const { page, log } = ctx;
			const worker = await startClaudeAuthWorker(ctx);
			const origin = `http://localhost:${worker.app.local_port}`;
			const initial = await request(page, origin, 'GET', statusPath);
			if (initial.accounts.subscription.length || initial.accounts.login_pending) {
				throw new Error('case must start without saved or pending subscriptions');
			}
			log('started without saved or pending subscriptions');
			if (existing) {
				const { login } = await request(page, origin, 'POST', `${auth}/accounts/subscription/add-another`);
				const folder = realpathSync(login.env.CLAUDE_CONFIG_DIR);
				if (dirname(folder) !== join(realpathSync(worker.data_dir), 'claude-logins')) {
					throw new Error('fixture sign-in folder is outside the disposable worker');
				}
				writeFileSync(join(folder, 'devloop-account.txt'), 'fixture-extra', { flag: 'wx' });
				// Complete the add before we check the navigation invariant.
				const saved = await request(page, origin, 'POST', `${auth}/accounts/subscription/cancel-login`);
				if (!saved.account_id) throw new Error('Extra sign-in was not saved');
			}
			const folder = join(worker.scratch_dir, 'home', '.claude');
			mkdirSync(folder, { recursive: true });
			if (realpathSync(folder) !== join(realpathSync(worker.scratch_dir), 'home', '.claude')) {
				throw new Error('stock sign-in folder is outside the disposable worker');
			}
			// Exclusively create a fake-only marker (empty means signed out). Keep
			// the descriptor so external login never overwrites a path we did not create.
			const marker = openSync(join(folder, 'devloop-account.txt'), 'wx', 0o600);
			try {
				await request(page, origin, 'PUT', auth, { mode: 'off' });
				await request(page, origin, 'PUT', auth, { mode: 'subscription' });
				const state = await request(page, origin, 'GET', statusPath);
				if (state.cli.signed_in || !state.accounts.recovery_required) throw new Error('subscription must start unavailable');
				const rows = state.accounts.subscription;
				if (existing ? rows.length !== 1 || rows[0].kind !== 'saved' : rows.length !== 0) {
					throw new Error('expected only the saved subscriptions created for this case');
				}
				log(`recovery required with ${rows.length} saved subscriptions`);
				writeFileSync(marker, 'recovered-fixture');
			} finally {
				closeSync(marker);
			}
			await gotoApp(page, { urls: { ui: origin } }, '/');
			const sidebar = await showSidebar(page);
			await sidebar.getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('button', { name: 'Settings', exact: true }).click();
			await page.getByRole('navigation', { name: 'Settings', exact: true }).getByRole('button', { name: 'Providers', exact: true }).click();
			await page.locator('[data-settings-item="provider:claude"]:visible').first().click();
			await page.getByText(/Update all workers before relying on a switch/).waitFor();
			await page.getByRole('button', { name: /^Open Claude(?: .+)? subscription$/i }).click();
			await page.getByRole('button', { name: 'Use this sign-in', exact: true }).waitFor();
		},
		async act({ page, mark }) {
			await mark('verify and select the default CLI sign-in after external login');
			const [changed] = await Promise.all([
				page.waitForResponse((res) => res.url().endsWith(`${auth}/accounts/subscription/use-default`)),
				page.getByRole('button', { name: 'Use this sign-in', exact: true }).click(),
			]);
			if (!changed.ok()) throw new Error('verified default selection failed');
			const state = await request(page, new URL(page.url()).origin, 'GET', statusPath);
			if (!state.cli.signed_in || state.cli.email !== 'recovered-fixture@devloop.invalid' ||
				!state.gate.claude_credential_ready || state.accounts.recovery_required) {
				throw new Error(`default selection: signed in=${state.cli.signed_in}, gate=${state.gate.claude_credential_ready}, recovery=${state.accounts.recovery_required}`);
			}
		}
	}))
});
