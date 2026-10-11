// Screenshots of tests/chat-controls.html at 390x844 for review (#1816).
// Needs browser-mcp's playwright-core (`cd ../browser-mcp && npm ci`) and a
// Chromium (CHROMIUM_PATH). Serve the fixture first:
//   npx vite dev --config tests/vite.chat-controls.config.js --port 5191 --strictPort
//   CHROMIUM_PATH=... node tests/shoot-chat-controls.mjs [outDir] [scenario...]
import { createRequire } from 'node:module';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';

const require = createRequire(new URL('../../browser-mcp/package.json', import.meta.url));
const { chromium } = require('playwright-core');
const BASE = process.env.FIXTURE_URL ?? 'http://localhost:5191/mobile/tests/chat-controls.html';
const out = resolve(process.argv[2] ?? 'tests/.shots');
const only = process.argv.slice(3);
mkdirSync(out, { recursive: true });

const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH, headless: true });
const errors = [];

async function open(query = '', { height = 844, reducedMotion = 'reduce' } = {}) {
	const page = await browser.newPage({ viewport: { width: 390, height }, deviceScaleFactor: 2, hasTouch: true, reducedMotion });
	page.on('pageerror', (e) => errors.push(`${query}: ${e.message}`));
	page.on('console', (m) => { if (m.type() === 'error') errors.push(`${query}: ${m.text()}`); });
	await page.goto(`${BASE}${query}`);
	await page.waitForSelector('.qbar', { timeout: 20000 });
	await page.waitForTimeout(600);
	return page;
}
const shot = (page, name) => page.screenshot({ path: `${out}/${name}.png` });

const scenarios = {
	async rest(theme) {
		const page = await open(`?theme=${theme}`);
		await shot(page, `${theme}-1-rest`);
		await page.getByRole('button', { name: /^Reasoning effort/ }).click();
		await page.waitForTimeout(300);
		await shot(page, `${theme}-2-effort-picker`);
		await page.keyboard.press('Escape');
		await page.getByRole('button', { name: /^Tool access/ }).click();
		await page.waitForTimeout(300);
		await shot(page, `${theme}-3-access-picker`);
		await page.keyboard.press('Escape');
		await page.getByRole('button', { name: /^All chat controls/ }).click();
		await page.waitForTimeout(500);
		await shot(page, `${theme}-4-panel`);
		await page.locator('.mcp-body').evaluate((el) => { el.scrollTop = 0; });
		await page.waitForTimeout(200);
		await shot(page, `${theme}-5-panel-top`);
		await page.getByRole('button', { name: /^Model:/ }).last().click();
		await page.waitForTimeout(500);
		await shot(page, `${theme}-6-model`);
		await page.close();
	},
	async keyboard(theme) {
		const page = await open(`?theme=${theme}`, { height: 520 });
		await page.locator('.ProseMirror').click();
		await page.keyboard.type('Also cover the AppImage permissions step and the desktop entry file for GNOME and KDE users who install by hand');
		await page.waitForTimeout(300);
		await shot(page, `${theme}-7-keyboard-up`);
		await page.close();
	},
	async prompts(theme) {
		const page = await open(`?theme=${theme}&prompt=all`);
		await shot(page, `${theme}-8-prompt-1of4`);
		const knob = page.getByRole('slider').first();
		const box = await knob.boundingBox();
		await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
		await page.mouse.down();
		await page.mouse.move(box.x + box.width / 2 + 90, box.y + box.height / 2, { steps: 8 });
		await shot(page, `${theme}-9-sliding`);
		await page.mouse.move(box.x + box.width / 2 + 160, box.y + box.height / 2, { steps: 4 });
		await page.mouse.up();
		await page.waitForTimeout(300);
		await shot(page, `${theme}-10-approved`);
		await page.waitForTimeout(1200);
		await shot(page, `${theme}-11-next-access`);
		await page.getByRole('button', { name: /of \d+\. Show the next/ }).click();
		await page.getByRole('button', { name: /of \d+\. Show the next/ }).click();
		await page.waitForTimeout(400);
		await shot(page, `${theme}-12-question`);
		await page.close();
	},
	async runsOn(theme) {
		const page = await open(`?theme=${theme}`);
		await page.getByRole('button', { name: /^All chat controls/ }).click();
		await page.waitForTimeout(500);
		await page.getByRole('button', { name: /^Runner:/ }).click();
		await page.waitForTimeout(600);
		await shot(page, `${theme}-13-runs-on-menu`);
		const unraid = page.getByRole('menuitem', { name: 'Unraid' });
		if (await unraid.count()) { await unraid.click(); await page.waitForTimeout(600); }
		await shot(page, `${theme}-14-runs-on-busy`);
		await page.close();
	},
	async assistant(theme) {
		const page = await open(`?theme=${theme}&view=assistant&prompt=none`);
		await shot(page, `${theme}-15-assistant`);
		await page.getByRole('button', { name: /^All chat controls/ }).click();
		await page.waitForTimeout(500);
		await shot(page, `${theme}-16-assistant-panel`);
		await page.close();
	},
	async conn(theme) {
		const page = await open(`?theme=${theme}&prompt=none`);
		await page.getByRole('button', { name: /^Connected to/ }).click();
		await page.waitForTimeout(500);
		await shot(page, `${theme}-17-connected-to`);
		await page.close();
	},
	async usage(theme) {
		const page = await open(`?theme=${theme}&prompt=none`);
		await page.getByRole('button', { name: /^Context and usage/ }).first().click();
		await page.waitForTimeout(500);
		await shot(page, `${theme}-18-usage`);
		await page.close();
	},
	async drag(theme) {
		const page = await open(`?theme=${theme}&prompt=none`, { reducedMotion: 'no-preference' });
		const bar = await page.locator('.qbar').boundingBox();
		const x = bar.x + bar.width / 2, y = bar.y + 8;
		await page.mouse.move(x, y);
		await page.mouse.down();
		await page.mouse.move(x, y - 160, { steps: 10 });
		await shot(page, `${theme}-19-drag-half`);
		await page.mouse.move(x, y - 420, { steps: 6 });
		await page.mouse.up();
		await page.waitForTimeout(600);
		await shot(page, `${theme}-20-drag-open`);
		const step = await page.getByRole('button', { name: /^Close chat controls/ }).isVisible();
		console.log('panel open after drag:', step);
		await page.close();
	},
	// #2274: the context meter on a provider model, unknown window vs a server-resolved one.
	async contextBound(theme) {
		for (const bound of ['unknown', 'server']) {
			const page = await open(`?theme=${theme}&prompt=none&bound=${bound}`);
			const label = await page.locator('.qcell.usage .usage-indicator').getAttribute('aria-label');
			const quick = await page.locator('.qcell.usage .usage-percent').textContent();
			console.log(`${bound}: quick bar "${quick}", label "${label}"`);
			await shot(page, `${theme}-21-context-${bound}-bar`);
			await page.getByRole('button', { name: /^All chat controls/ }).click();
			await page.waitForTimeout(500);
			console.log(`${bound}: panel row "${await page.locator('.mcp .usage-row .pct').textContent()}"`);
			await shot(page, `${theme}-22-context-${bound}-panel`);
			await page.locator('.mcp .usage-row').click();
			await page.waitForTimeout(800);
			await shot(page, `${theme}-23-context-${bound}-details`);
			await page.close();
		}
	}
};

for (const theme of ['dark', 'vercel']) {
	for (const [name, run] of Object.entries(scenarios)) {
		if (only.length && !only.includes(name)) continue;
		try { await run(theme); } catch (e) { errors.push(`${theme}/${name}: ${e.message.split('\n')[0]}`); }
	}
}
await browser.close();
console.log(errors.length ? `errors:\n${errors.join('\n')}` : 'no page errors');
