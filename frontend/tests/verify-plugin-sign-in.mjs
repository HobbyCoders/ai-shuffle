// Real-browser check of plugin service sign-in (OAuth) on both surfaces: the
// private access card for a plugin request and Settings > Plugins >
// Connections. Every state the worker reports, the provider window (a fake
// provider that redirects to a faked callback page posting the same
// `plugin-oauth` message the worker's page posts), the focus fallback when no
// message arrives, inline sign-out, and the manual client setup. Desktop and
// a 390px phone width; at 390 the mobile shell's stylesheet is applied too.
// Nothing reaches a real worker or provider: the API is answered here.
//
// Run from frontend/ while the fixtures are serving:
//   npx vite dev --config tests/vite.plugin-sign-in.config.js --host 127.0.0.1 --port 6931 --strictPort
//   node tests/verify-plugin-sign-in.mjs
// Needs browser-mcp's playwright-core (npm ci --ignore-scripts --prefix ../browser-mcp)
// and a Chrome/Chromium binary (CHROMIUM_PATH). PLUGIN_SIGN_IN_FIXTURE_ORIGIN
// overrides the origin; SCREENSHOT_DIR saves a screenshot per state.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { createRequire } from 'node:module';
const require = createRequire(new URL('../../browser-mcp/package.json', import.meta.url));
const { chromium } = require('playwright-core');
const browser = await chromium.launch({
	executablePath: process.env.CHROMIUM_PATH || {
		win32: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
		darwin: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
	}[process.platform] || '/usr/bin/chromium',
	headless: true,
	args: ['--no-sandbox', '--disable-gpu'],
});
const origin = process.env.PLUGIN_SIGN_IN_FIXTURE_ORIGIN || 'http://127.0.0.1:6931';
const shots = process.env.SCREENSHOT_DIR;
const mobileCss = readFileSync(new URL('../../mobile/src/mobile.css', import.meta.url), 'utf8');
const PLUGIN = 'dns-reader@mp';
const CALLBACK = '/api/v1/plugins/oauth/callback';
const SECRET = 'fixture-only-client-secret';

const entry = (overrides = {}) => ({
	// The PR #1762 shape: client_source, plugin_client_id and registration_url.
	plugin_id: PLUGIN, service: 'cloudflare', state: 'signed_out', reason: '', client_configured: true, client_id: 'cf-client-id',
	client_source: 'owner', plugin_client_id: '', registration_url: '',
	requested_scopes: ['zone:read', 'dns_records:read'], scopes: [], missing_scopes: [], expires_at: null, can_refresh: false,
	access: 'read', authorize_host: 'dash.cloudflare.com', ...overrides,
});
const pluginInfo = {
	id: PLUGIN, name: 'dns-reader', marketplace: 'mp', description: 'Reads DNS zones', version: '1.2.0', scope: 'user',
	install_path: '/home/user/.fluid/plugins/cache/dns-reader', installed_at: '2026-10-01T00:00:00', enabled: true,
	has_commands: false, has_agents: false, has_skills: true, has_hooks: false, has_mcp: false, connector_type: null,
	format: 'agent-plugins', trust: 'marketplace', approval: null, content_hash: null,
	components: [
		{ kind: 'skill', name: 'dns-audit', support: 'loaded', reason: '', detail: '' },
		{ kind: 'service', name: 'cloudflare', support: 'loaded', reason: '', detail: '' },
		{ kind: 'service', name: 'github', support: 'loaded', reason: '', detail: '' },
	],
};

let passed = 0;

/** Where the worker takes the client from, as app/core/plugin_oauth.client_source does. */
const sourceOf = (s) => (s.client_configured ? 'owner' : s.plugin_client_id ? 'plugin' : s.registration_url ? 'registered' : 'none');

/** The fake worker and provider for one browser context. `w` is the scenario's world. */
async function fake(context, w) {
	await context.route((u) => u.hostname === 'auth.provider.test', (route) => {
		const state = new URL(route.request().url()).searchParams.get('state');
		const back = `${origin}${CALLBACK}?state=${state}&${w.provider === 'deny' ? 'error=access_denied' : 'code=fixture'}`;
		return route.fulfill({ contentType: 'text/html', body: `<!doctype html><title>Provider</title><p>Signing in…</p><script>setTimeout(() => location.replace(${JSON.stringify(back)}), 150)</script>` });
	});
	await context.route((u) => u.pathname.startsWith('/api/'), async (route) => {
		const request = route.request();
		const url = new URL(request.url());
		const path = decodeURIComponent(url.pathname.replace('/api/v1', ''));
		const method = request.method();
		if (method !== 'GET') w.sent.push({ method, path, body: request.postData() ? request.postDataJSON() : null });
		const service = (name) => w.services.find((s) => s.service === name);
		if (path === '/plugins/oauth/callback') {
			// The worker's callback page: no code or token, posts to its opener and closes.
			const s = service(w.flowService ?? 'cloudflare');
			let error = null;
			if (w.provider === 'deny') error = 'The sign-in was cancelled at the service. Nothing was changed.';
			else Object.assign(s, { state: 'signed_in', reason: '', scopes: s.requested_scopes });
			const message = { type: 'plugin-oauth', flow_id: 'flow-1', plugin_id: PLUGIN, service: s.service, state: error ? null : s.state, error };
			const post = w.provider === 'silent' ? 'window.close();' : `if(window.opener){window.opener.postMessage(${JSON.stringify(message)},${JSON.stringify(origin)});window.close();}`;
			return route.fulfill({ contentType: 'text/html', headers: { 'Cache-Control': 'no-store' }, body: `<!doctype html><meta charset="utf-8"><title>Signed in</title><h1>Done</h1><script>${post}</script>` });
		}
		if (path === '/auth/status') return route.fulfill({ json: { authenticated: true, setup_required: false, is_admin: true, username: 'owner' } });
		if (path === '/sessions/fixture-session') return route.fulfill({ json: { mode: 'chat' } });
		if (path === '/chat-access/fixture-session') return route.fulfill({ json: { grants: [], actions: [], requests: w.requests, saved_connections: [], vault_available: true } });
		if (path === '/chat-access/fixture-session/requests/r1') { w.requests = []; return route.fulfill({ json: { granted: true, saved: false } }); }
		if (path === '/plugins/oauth/status') {
			const only = url.searchParams.get('service');
			return route.fulfill({ json: { plugin_id: PLUGIN, enabled: w.enabled, redirect_uri: `${origin}${CALLBACK}`, services: w.services.filter((s) => !only || s.service === only) } });
		}
		if (path === '/plugins/oauth/start') {
			if (!w.enabled) return route.fulfill({ status: 409, json: { detail: 'Turn this plugin on in Settings > Plugins before signing in to its services.' } });
			w.flowService = request.postDataJSON().service;
			const s = service(w.flowService);
			if (w.registration === 'refuse' && s.client_source === 'registered') {
				// The provider refused the registration: 409, and the service reads
				// not_configured / registration_failed until the owner enters a client.
				Object.assign(s, { state: 'not_configured', reason: 'registration_failed', client_source: 'none' });
				return route.fulfill({ status: 409, json: { detail: 'The service did not accept automatic sign-in setup. Enter an OAuth client for it in Settings > Plugins.' } });
			}
			return route.fulfill({ json: { authorization_url: 'https://auth.provider.test/authorize?state=fixture-state', flow_id: 'flow-1', redirect_uri: `${origin}${CALLBACK}`, expires_in: 600 } });
		}
		if (path === '/plugins/oauth/client' && method === 'PUT') {
			const body = request.postDataJSON();
			Object.assign(service(body.service), { state: 'signed_out', reason: '', client_configured: true, client_id: body.client_id, client_source: 'owner' });
			return route.fulfill({ json: { ...service(body.service) } });
		}
		if (path === '/plugins/oauth/client' && method === 'DELETE') {
			const s = Object.assign(service(url.searchParams.get('service')), { client_configured: false, client_id: '', scopes: [] });
			Object.assign(s, { client_source: sourceOf(s), state: sourceOf(s) === 'none' ? 'not_configured' : 'signed_out', reason: '' });
			return route.fulfill({ json: { deleted: true } });
		}
		if (path === '/plugins/oauth/sign-out') {
			Object.assign(service(request.postDataJSON().service), { state: 'signed_out', scopes: [] });
			return route.fulfill({ json: { signed_out: true, revoked: true } });
		}
		if (path === '/plugins/installed') return route.fulfill({ json: [{ ...pluginInfo, enabled: w.enabled }] });
		if (path === `/plugins/${PLUGIN}`) return route.fulfill({ json: { ...pluginInfo, enabled: w.enabled, commands: [], agents: [], skills: ['dns-audit'], readme: null } });
		if (['/plugins/available', '/plugins/marketplaces', '/plugins/updates', '/projects'].includes(path) || path.startsWith('/connectors')) return route.fulfill({ json: [] });
		if (path === '/plugins/auto-update/settings') return route.fulfill({ json: { enabled: false, interval_hours: 24, auto_sync_marketplaces: true, auto_update_plugins: false, last_check: null } });
		return route.fulfill({ status: 404, json: { detail: 'not in this fixture' } });
	});
}

const request = { id: 'r1', kind: 'service', purpose: 'List the DNS records in the example.com zone', expires_at: 9999999999, status: 'awaiting_user',
	suggested_url: 'https://api.cloudflare.com/client/v4', plugin: PLUGIN, service: 'cloudflare' };

async function run(viewport, touch) {
	const name = `${viewport.width}x${viewport.height}`;
	const context = await browser.newContext({ viewport, isMobile: touch, hasTouch: touch, deviceScaleFactor: 1, serviceWorkers: 'block' });
	const w = { sent: [], requests: [], services: [], enabled: true, provider: 'approve' };
	await fake(context, w);
	const page = await context.newPage();
	const errors = [];
	page.on('pageerror', (e) => errors.push(e.message));
	let label = '';
	const check = (condition, what, detail) => {
		assert.ok(condition, `[${name} ${label}] ${what}${detail === undefined ? '' : `: ${JSON.stringify(detail)}`}`);
		passed++;
	};
	const shot = async (file) => { if (shots) await page.screenshot({ path: join(shots, `${name}-${file}.png`), fullPage: true }); };
	const phoneShell = async () => {
		if (!touch) return;
		await page.evaluate((css) => {
			document.documentElement.dataset.platform = 'mobile';
			const style = document.createElement('style');
			style.textContent = css;
			document.head.append(style);
		}, mobileCss);
	};
	const noOverflow = (selector) => page.evaluate((selector) => {
		const doc = document.documentElement.scrollWidth - document.documentElement.clientWidth;
		const el = selector ? document.querySelector(selector) : null;
		return Math.max(doc, el ? el.scrollWidth - el.clientWidth : 0);
	}, selector);
	const reset = (services, extra = {}) => Object.assign(w, { sent: [], requests: [{ ...request }], services, enabled: true, provider: 'approve', registration: 'accept' }, extra);

	// ── The access card ────────────────────────────────────────────────────
	const openCard = async () => {
		await page.goto(`${origin}/tests/chat-access.html?session=fixture-session`);
		await phoneShell();
		const button = page.getByRole('button', { name: 'Private access' }).first();
		await button.waitFor();
		if (touch) await button.tap(); else await button.click();
		const card = page.getByRole('region', { name: 'Sign in to cloudflare' });
		await card.waitFor();
		return card;
	};
	const popupFrom = async (click) => {
		const [popup] = await Promise.all([context.waitForEvent('page'), click()]);
		return popup;
	};

	label = 'card signed out';
	reset([entry()]);
	let card = await openCard();
	const signIn = card.getByRole('button', { name: 'Sign in with dash.cloudflare.com' });
	await signIn.waitFor();
	check(await card.getByText('dns-reader', { exact: true }).isVisible(), 'plugin name shown');
	check(await card.getByText('Read-only', { exact: true }).isVisible(), 'declared access shown');
	check(await card.getByText('Uses your client').isVisible() && await card.getByText('cf-client-id').isVisible(), 'owner client named');
	check(!(await card.locator('details.ps-scopes').evaluate((d) => d.open)), 'scopes collapsed');
	check((await card.getByText('API key or token').count()) === 0, 'no key form');
	check((await card.getByRole('button', { name: 'Grant access' }).count()) === 0, 'no grant before sign-in');
	check((await noOverflow('dialog.access-dialog')) <= 0, 'fits', await noOverflow('dialog.access-dialog'));
	await shot('card-1-signed-out');
	const popup = await popupFrom(() => (touch ? signIn.tap() : signIn.click()));
	await card.getByRole('button', { name: 'Waiting…' }).waitFor();
	check(await card.getByText(/in the window that opened/).isVisible(), 'progress while the provider window is open');
	await shot('card-2-waiting');
	await popup.waitForEvent('close');
	const grant = card.getByRole('button', { name: 'Grant access' });
	await grant.waitFor();
	check(await card.getByText('Signed in. Grant access to finish.').isVisible(), 'success line');
	check(await grant.evaluate((b) => document.activeElement === b), 'focus moves to Grant access');
	await shot('card-3-signed-in');
	if (touch) await grant.tap(); else await grant.click();
	await page.waitForFunction(() => !document.querySelector('[aria-label="Sign in to cloudflare"]') || /Request completed/.test(document.body.textContent || ''));
	const granted = w.sent.find((s) => s.path === '/chat-access/fixture-session/requests/r1');
	check(JSON.stringify(granted?.body) === JSON.stringify({ sign_in: true, writable: false, minutes: 60 }), 'grant is sign_in only', granted);

	label = 'card not configured';
	reset([entry({ state: 'not_configured', client_configured: false, client_id: '', client_source: 'none' })]);
	card = await openCard();
	const manual = card.getByText('Set up manually');
	await manual.waitFor();
	check((await card.getByRole('button', { name: /Sign in with/ }).count()) === 0, 'no sign-in button without a client');
	check(!(await card.locator('details.ps-manual').evaluate((d) => d.open)), 'setup tucked away');
	await shot('card-4-needs-setup');
	await manual.click();
	check(await card.getByText(`${origin}${CALLBACK}`).isVisible(), 'return address shown');
	await card.getByLabel('Client ID').fill('my-cloudflare-app');
	await card.getByLabel(/Client secret/).fill(SECRET);
	check((await noOverflow('dialog.access-dialog')) <= 0, 'setup fits', await noOverflow('dialog.access-dialog'));
	await shot('card-5-setup-open');
	await card.getByRole('button', { name: 'Save client' }).click();
	await card.getByRole('button', { name: 'Sign in with dash.cloudflare.com' }).waitFor();
	const saved = w.sent.find((s) => s.path === '/plugins/oauth/client');
	check(saved?.body?.client_id === 'my-cloudflare-app' && saved?.body?.client_secret === SECRET, 'client saved once', saved?.body?.client_id);
	check(!(await page.content()).includes(SECRET), 'secret not rendered back');

	label = 'card plugin client';
	reset([entry({ client_configured: false, client_id: '', client_source: 'plugin', plugin_client_id: 'dns-reader-public-client-0a1b2c3d4e5f' })]);
	card = await openCard();
	const shippedSignIn = card.getByRole('button', { name: 'Sign in with dash.cloudflare.com' });
	await shippedSignIn.waitFor();
	check(await card.getByText('Uses the plugin’s client').isVisible(), 'plugin client named');
	check((await card.getByText('Set up manually').count()) === 0, 'no manual setup with a shipped client');
	check((await noOverflow('dialog.access-dialog')) <= 0, 'fits', await noOverflow('dialog.access-dialog'));
	await shot('card-5b-plugin-client');
	const shippedPopup = await popupFrom(() => (touch ? shippedSignIn.tap() : shippedSignIn.click()));
	await shippedPopup.waitForEvent('close');
	await card.getByRole('button', { name: 'Grant access' }).waitFor();
	check(true, 'signs in with the shipped client, no setup');

	label = 'card automatic setup refused';
	reset([entry({ client_configured: false, client_id: '', client_source: 'registered', registration_url: 'https://dash.cloudflare.com/oauth2/register' })], { registration: 'refuse' });
	card = await openCard();
	const regSignIn = card.getByRole('button', { name: 'Sign in with dash.cloudflare.com' });
	await regSignIn.waitFor();
	check(await card.getByText('Set up automatically with').isVisible(), 'automatic setup named');
	await shot('card-5c-registered');
	await regSignIn.click();
	await card.getByText(/Automatic setup didn’t work/).waitFor();
	check(await card.getByText('Set up manually').isVisible(), 'manual setup offered after a refused registration');
	check((await card.getByRole('button', { name: /Sign in with/ }).count()) === 0, 'no sign-in until a client is entered');
	check(context.pages().length === 1, 'provider window closed');
	await shot('card-5d-registration-failed');

	label = 'card needs sign-in';
	reset([entry({ state: 'needs_sign_in', reason: 'expired' })]);
	card = await openCard();
	await card.getByText('Your sign-in expired. Sign in again.').waitFor();
	check(await card.getByRole('button', { name: 'Sign in with dash.cloudflare.com' }).isVisible(), 'sign in again offered');
	await shot('card-6-expired');

	label = 'card cancelled at provider';
	reset([entry()], { provider: 'deny' });
	card = await openCard();
	const again = card.getByRole('button', { name: 'Sign in with dash.cloudflare.com' });
	await again.waitFor();
	const denied = await popupFrom(() => again.click());
	await denied.waitForEvent('close');
	await card.getByText('The sign-in was cancelled at the service. Nothing was changed.').waitFor();
	check(await again.isVisible(), 'can try again after cancelling');
	await shot('card-7-cancelled');

	label = 'card no message, write access';
	reset([entry({ access: 'write' })], { provider: 'silent' });
	card = await openCard();
	const silentButton = card.getByRole('button', { name: 'Sign in with dash.cloudflare.com' });
	await silentButton.waitFor();
	const silent = await popupFrom(() => silentButton.click());
	await silent.waitForEvent('close');
	await page.evaluate(() => window.dispatchEvent(new Event('focus')));
	await card.getByRole('button', { name: 'Grant access' }).waitFor();
	check(true, 'focus re-reads the status when no message arrives');
	await card.getByRole('checkbox', { name: /Allow changes/ }).check();
	check(await card.getByText('Read and write', { exact: true }).isVisible(), 'read and write shown');
	await shot('card-8-write');
	await card.getByRole('button', { name: 'Grant access' }).click();
	await page.waitForTimeout(200);
	check(w.sent.find((s) => s.path === '/chat-access/fixture-session/requests/r1')?.body?.writable === true, 'writable only when declared');

	label = 'card plugin off';
	reset([entry()], { enabled: false });
	card = await openCard();
	await card.getByText(/switched off/).waitFor();
	check((await card.getByRole('button', { name: /Sign in with|Grant access/ }).count()) === 0, 'no sign-in or grant');
	await shot('card-9-plugin-off');

	label = 'card invalid';
	reset([entry({ state: 'invalid', reason: 'invalid_declaration', client_configured: false, client_id: '', client_source: 'none' })]);
	card = await openCard();
	await card.getByText(/sign-in settings are broken/).waitFor();
	check((await noOverflow('dialog.access-dialog')) <= 0, 'fits', await noOverflow('dialog.access-dialog'));
	await shot('card-10-invalid');

	// ── Settings > Plugins > Connections ───────────────────────────────────
	const openSettings = async () => {
		await page.evaluate(() => sessionStorage.clear()).catch(() => {});
		await page.goto(`${origin}/tests/settings-plugins.html?tab=plugins&item=${encodeURIComponent(`plugin:${PLUGIN}`)}`);
		await phoneShell();
		const row = page.getByTestId('plugin-service-cloudflare');
		await row.waitFor();
		return row;
	};
	const github = entry({ service: 'github', state: 'invalid', reason: 'invalid_declaration', authorize_host: '', client_configured: false, client_id: '', client_source: 'none' });

	label = 'settings signed out';
	reset([entry(), github]);
	let row = await openSettings();
	check(await row.getByText('Not signed in · dash.cloudflare.com').isVisible(), 'state and host');
	check(await page.getByTestId('plugin-service-github').getByText('Sign-in declaration is invalid').isVisible(), 'invalid service');
	check((await noOverflow()) <= 0, 'fits', await noOverflow());
	await shot('settings-1-signed-out');
	const rowSignIn = row.getByRole('button', { name: 'Sign in with dash.cloudflare.com' });
	const rowPopup = await popupFrom(() => (touch ? rowSignIn.tap() : rowSignIn.click()));
	await rowPopup.waitForEvent('close');
	await row.getByText('Signed in · dash.cloudflare.com').waitFor();
	check(true, 'signs in from Settings');
	await shot('settings-2-signed-in');

	label = 'settings sign out';
	await row.getByRole('button', { name: 'Sign out' }).click();
	await row.getByText('Sign out of cloudflare?').waitFor();
	check((await page.locator('dialog[open]').count()) === 0, 'confirm is inline, no dialog');
	await shot('settings-3-confirm-sign-out');
	await row.getByRole('button', { name: 'Sign out' }).last().click();
	await row.getByText('Not signed in · dash.cloudflare.com').waitFor();
	check(w.sent.some((s) => s.path === '/plugins/oauth/sign-out' && s.body.service === 'cloudflare'), 'sign-out sent');

	label = 'settings not configured';
	reset([entry({ state: 'not_configured', client_configured: false, client_id: '', client_source: 'none' }), github]);
	row = await openSettings();
	check(await row.getByText('Needs one-time setup').isVisible(), 'needs setup hint');
	check((await row.getByRole('button', { name: /Sign in/ }).count()) === 0, 'no sign-in without a client');
	await row.getByRole('button', { name: 'Set up manually' }).click();
	await page.getByLabel('Client ID').fill('my-cloudflare-app');
	check((await noOverflow()) <= 0, 'setup fits', await noOverflow());
	await shot('settings-4-setup');
	await page.getByRole('button', { name: 'Save client' }).click();
	await row.getByRole('button', { name: 'Sign in with dash.cloudflare.com' }).waitFor();
	check(w.sent.some((s) => s.path === '/plugins/oauth/client' && s.body.client_id === 'my-cloudflare-app' && !('client_secret' in s.body)), 'public client saved without a secret');

	label = 'settings needs sign-in';
	reset([entry({ state: 'needs_sign_in', reason: 'scopes_widened' })]);
	row = await openSettings();
	check(await row.getByText('Asks for more access').isVisible(), 'reason hint');
	check((await row.getByRole('button', { name: 'Sign in with dash.cloudflare.com' }).textContent()) === 'Sign in again', 'sign in again');
	await shot('settings-5-needs-sign-in');

	label = 'settings automatic setup';
	reset([entry({ client_configured: false, client_id: '', client_source: 'registered', registration_url: 'https://dash.cloudflare.com/oauth2/register' })], { registration: 'refuse' });
	row = await openSettings();
	check(await row.getByText('Not signed in · dash.cloudflare.com').isVisible(), 'ready without setup');
	check((await row.getByRole('button', { name: /Set up manually|Change client/ }).count()) === 0, 'no manual setup while automatic setup is possible');
	await row.getByRole('button', { name: 'Sign in with dash.cloudflare.com' }).click();
	await row.getByText('Automatic setup failed').waitFor();
	check(await row.getByText(/did not accept automatic sign-in setup/).isVisible(), 'worker refusal shown');
	check(await row.getByRole('button', { name: 'Set up manually' }).isVisible(), 'manual setup offered');
	check((await noOverflow()) <= 0, 'fits', await noOverflow());
	await shot('settings-5b-registration-failed');

	label = 'settings change client';
	reset([entry({ state: 'needs_sign_in', reason: 'client_changed', plugin_client_id: 'dns-reader-public-client' })]);
	row = await openSettings();
	await row.getByRole('button', { name: 'Change client' }).click();
	await page.getByText('Without it, sign-in uses the plugin’s own client.').waitFor();
	check((await noOverflow()) <= 0, 'fits', await noOverflow());
	await shot('settings-5c-change-client');
	await row.getByRole('button', { name: 'Remove' }).click();
	await row.getByRole('button', { name: 'Remove' }).last().click();
	await row.getByText('Not signed in · dash.cloudflare.com').waitFor();
	check((await row.getByRole('button', { name: /Set up manually|Change client/ }).count()) === 0, 'falls back to the plugin client');

	label = 'settings plugin off';
	reset([entry()], { enabled: false });
	row = await openSettings();
	check(await row.getByText('Turn the plugin on').isVisible(), 'plugin off hint');
	check((await row.getByRole('button', { name: /Sign in/ }).count()) === 0, 'no sign-in while off');
	await shot('settings-6-plugin-off');

	label = 'all';
	check(errors.length === 0, 'no page errors', errors);
	await context.close();
}

try {
	await run({ width: 1280, height: 800 }, false);
	await run({ width: 390, height: 844 }, true);
	console.log(`plugin-sign-in: ${passed} checks passed`);
} finally {
	await browser.close();
}
