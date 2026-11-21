// Screenshots of the Vault in every state the owner sees in a chat: idle
// (nothing, or only saved sign-ins), a request for a machine, a folder and a
// web service (API token, and username and password), a one-shot approval and
// active access — with the panel closed and open, at desktop (1440x900) and
// phone (390x844) widths, in dark, light and one preset theme. The API is
// answered here; the real components run unchanged.
//
// Run from frontend/ while the fixture is serving:
//   npx vite dev --config tests/vite.vault.config.js --host 127.0.0.1 --port 6917 --strictPort
//   SCREENSHOT_DIR=/tmp/shots node tests/shoot-vault.mjs
// Needs browser-mcp's playwright-core (npm ci --ignore-scripts --prefix ../browser-mcp)
// and a Chrome/Chromium binary (CHROMIUM_PATH). ONLY=<state> limits the run.
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { createRequire } from 'node:module';
const require = createRequire(new URL('../../browser-mcp/package.json', import.meta.url));
const { chromium } = require('playwright-core');
const shots = process.env.SCREENSHOT_DIR;
if (!shots) throw new Error('Set SCREENSHOT_DIR');
mkdirSync(shots, { recursive: true });
const base = process.env.VAULT_FIXTURE_URL || 'http://127.0.0.1:6917/tests/vault.html';
const browser = await chromium.launch({
	executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium',
	headless: true,
	args: ['--no-sandbox', '--disable-gpu'],
});
const far = 9999999999;
const soon = () => Math.floor(Date.now() / 1000) + 4 * 60;
const empty = () => ({ grants: [], actions: [], requests: [], saved_connections: [], vault_available: true, folder_available: true, password_sign_in: true });
const nas = { id: 'c1', label: 'NAS', target: 'admin@10.0.0.5:22', fingerprint: 'SHA256:4mF2vYq1x0Jt3kz', kind: 'machine' };
const states = {
	'idle-empty': () => empty(),
	'idle-saved': () => ({ ...empty(), saved_connections: [nas] }),
	'request': () => ({ ...empty(), requests: [{ id: 'r1', kind: 'machine', purpose: 'Read the backup job logs on the NAS to find why last night’s run failed.', expires_at: soon() }] }),
	'request-saved': () => ({ ...empty(), saved_connections: [nas, { id: 'c2', label: 'Build box', target: 'ci@10.0.0.9:22', fingerprint: 'SHA256:9pQ7cLw2e5Ru8mn', kind: 'machine' }],
		requests: [{ id: 'r1', kind: 'machine', purpose: 'Read the backup job logs on the NAS to find why last night’s run failed.', expires_at: soon(), suggested_host: '10.0.0.5', suggested_username: 'admin' }] }),
	'folder-request': () => ({ ...empty(), requests: [{ id: 'r3', kind: 'folder', purpose: 'Read the meeting notes to summarise this week.', expires_at: soon(), suggested_path: '/home/me/notes' }] }),
	'service-password': () => ({ ...empty(), requests: [{ id: 'r4', kind: 'service', purpose: 'List the open tickets in the help desk', expires_at: far, service_provider: 'custom', suggested_url: 'https://desk.example.com/api' }] }),
	'service-request': () => ({ ...empty(), requests: [{ id: 'r2', kind: 'service', purpose: 'List DNS records for hobbycoders.com', expires_at: far, service_provider: 'cloudflare', suggested_url: 'https://api.cloudflare.com/client/v4' }] }),
	'approval': () => ({ ...empty(),
		grants: [{ id: 'g1', label: 'NAS', kind: 'machine', target: 'admin@10.0.0.5:22', expires_at: far, writable: true, fingerprint: 'SHA256:4mF2vYq1x0Jt3kz' }],
		actions: [{ id: 'a1', grant_id: 'g1', operation: 'command', arguments: { command: 'journalctl -u backup.service --since yesterday | tail -50' }, expires_at: far }] }),
	'active': () => ({ ...empty(),
		grants: [
			{ id: 'g1', label: 'Notes', kind: 'folder', target: '/home/me/notes', expires_at: far, writable: false },
			{ id: 'g2', label: 'Cloudflare DNS', kind: 'service', target: 'https://api.cloudflare.com/client/v4', expires_at: far, writable: false, fingerprint: '' },
		],
		saved_connections: [nas] }),
};
// Steps taken on a waiting card before its screenshots, each saved as <tag>-<step>.png.
const drives = {
	'service-password': [
		['method', (card) => card.getByRole('radio', { name: 'Username and password' }).check()],
		['prepare', (card) => card.getByRole('button', { name: 'Continue to setup' }).click()],
		['review', (card) => card.getByRole('button', { name: 'I have the sign-in' }).click()],
	],
};
const themes = (process.env.THEMES || 'dark,light,light:claude').split(',');
const viewports = [['desktop', { width: 1440, height: 900 }, false], ['phone', { width: 390, height: 844 }, true]];
const only = process.env.ONLY?.split(',');
let count = 0;
for (const themeSpec of themes) {
	const [mode, preset] = themeSpec.split(':');
	for (const [vpName, viewport, touch] of viewports) {
		for (const [state, make] of Object.entries(states)) {
			if (only && !only.includes(state)) continue;
			const context = await browser.newContext({ viewport, isMobile: touch, hasTouch: touch, deviceScaleFactor: 1, serviceWorkers: 'block', reducedMotion: 'reduce' });
			const snapshot = make();
			await context.route((u) => u.pathname.startsWith('/api/'), (route) => {
				const path = new URL(route.request().url()).pathname;
				if (path.endsWith('/v1/sessions/fixture-session')) return route.fulfill({ json: { mode: 'chat' } });
				if (path.endsWith('/vault/sessions/fixture-session')) return route.fulfill({ json: snapshot });
				return route.fulfill({ json: {} });
			});
			const page = await context.newPage();
			const errors = [];
			page.on('pageerror', (e) => errors.push(e.message));
			await page.goto(`${base}?session=fixture-session&theme=${mode}${preset ? `&preset=${preset}` : ''}`);
			await page.waitForTimeout(700);
			const tag = `${vpName}-${themeSpec.replace(':', '-')}-${state}`;
			await page.screenshot({ path: join(shots, `${tag}-closed.png`) });
			count++;
			for (const [step, run] of drives[state] ?? []) {
				await run(page.getByRole('region', { name: 'Vault request' }));
				await page.waitForTimeout(300);
				await page.screenshot({ path: join(shots, `${tag}-${step}.png`) });
				count++;
			}
			const trigger = page.getByRole('button', { name: /^Vault/ }).first();
			if (await trigger.count()) {
				if (touch) await trigger.tap(); else await trigger.click();
				await page.waitForTimeout(500);
				await page.screenshot({ path: join(shots, `${tag}-open.png`) });
				count++;
			}
			if (errors.length) console.error(tag, errors);
			await context.close();
		}
	}
}
await browser.close();
console.log(`saved ${count} screenshots to ${shots}`);
