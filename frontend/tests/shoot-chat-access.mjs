// Screenshots of the private-access UI in every state the owner sees: idle
// (nothing, or only saved connections), a pending request, a one-shot
// approval and active grants — closed and open, at desktop (1440x900) and
// phone (390x844) widths, in dark, light and one preset theme. The API is
// answered here; the real components run unchanged.
//
// Run from frontend/ while the fixture is serving:
//   npx vite dev --config tests/vite.chat-access.config.js --host 127.0.0.1 --port 6917 --strictPort
//   SCREENSHOT_DIR=/tmp/shots node tests/shoot-chat-access.mjs
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
const base = process.env.CHAT_ACCESS_FIXTURE_URL || 'http://127.0.0.1:6917/tests/chat-access.html';
const browser = await chromium.launch({
	executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium',
	headless: true,
	args: ['--no-sandbox', '--disable-gpu'],
});
const far = 9999999999;
const soon = () => Math.floor(Date.now() / 1000) + 4 * 60;
const empty = () => ({ grants: [], actions: [], requests: [], saved_connections: [], vault_available: true });
const states = {
	'idle-empty': () => empty(),
	'idle-saved': () => ({ ...empty(), saved_connections: [{ id: 'c1', label: 'NAS', target: 'admin@10.0.0.5:22', fingerprint: 'SHA256:4mF2vYq1x0Jt3kz', kind: 'machine' }] }),
	'request': () => ({ ...empty(), requests: [{ id: 'r1', kind: 'machine', purpose: 'Read the backup job logs on the NAS to find why last night’s run failed.', expires_at: soon() }] }),
	'service-request': () => ({ ...empty(), requests: [{ id: 'r2', kind: 'service', purpose: 'List DNS records for hobbycoders.com', expires_at: far, service_provider: 'cloudflare', suggested_url: 'https://api.cloudflare.com/client/v4' }] }),
	'approval': () => ({ ...empty(),
		grants: [{ id: 'g1', label: 'NAS', kind: 'machine', target: 'admin@10.0.0.5:22', expires_at: far, writable: true, fingerprint: 'SHA256:4mF2vYq1x0Jt3kz' }],
		actions: [{ id: 'a1', grant_id: 'g1', operation: 'command', arguments: { command: 'journalctl -u backup.service --since yesterday | tail -50' }, expires_at: far }] }),
	'active': () => ({ ...empty(),
		grants: [
			{ id: 'g1', label: 'Notes', kind: 'folder', target: '/home/me/notes', expires_at: far, writable: false },
			{ id: 'g2', label: 'Cloudflare DNS', kind: 'service', target: 'https://api.cloudflare.com/client/v4', expires_at: far, writable: false, fingerprint: '' },
		],
		saved_connections: [{ id: 'c1', label: 'NAS', target: 'admin@10.0.0.5:22', fingerprint: 'SHA256:4mF2vYq1x0Jt3kz', kind: 'machine' }] }),
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
				if (path.endsWith('/sessions/fixture-session')) return route.fulfill({ json: { mode: 'chat' } });
				if (path.endsWith('/chat-access/fixture-session')) return route.fulfill({ json: snapshot });
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
			const trigger = page.getByRole('button', { name: /^Private access/ }).first();
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
