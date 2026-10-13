// Real-browser check of the automation form's Model row at desktop width: an
// automation without a model of its own says it runs on the app default and
// names that model; picking a model shows it; an agent that still carries a
// model saved by an older version says so instead.
//
// Run from frontend/ while the fixture is serving:
//   npx vite dev --config tests/vite.automation-form.config.js --host 127.0.0.1 --port 6402 --strictPort
//   node tests/verify-automation-form.mjs
// Needs browser-mcp's playwright-core (npm ci --ignore-scripts --prefix ../browser-mcp)
// and a Chrome/Chromium binary (CHROMIUM_PATH). AUTOMATION_FORM_FIXTURE_URL
// overrides the URL; SCREENSHOT_DIR saves a screenshot per step.
import assert from 'node:assert/strict';
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
const base = process.env.AUTOMATION_FORM_FIXTURE_URL || 'http://127.0.0.1:6402/tests/automation-form.html';
const shots = process.env.SCREENSHOT_DIR;

const registry = {
	models: [
		{ value: 'haiku', label: 'Haiku', family: 'haiku', supports_1m: false },
		{ value: 'sonnet', label: 'Sonnet', family: 'sonnet', supports_1m: true },
		{ value: 'opus', label: 'Opus', family: 'opus', supports_1m: true },
	],
	default: 'sonnet',
	legacy_map: {},
	claude: { auth_kind: 'subscription', plan_type: null },
	providers: [
		{ id: 'openai-row', name: 'OpenAI', provider_type: 'openai', models: [{ id: 'gpt-5.6', label: 'GPT-5.6' }], default_model: 'gpt-5.6' },
	],
};

let passed = 0;
const check = (condition, label, detail) => {
	assert.ok(condition, `${label}${detail === undefined ? '' : `: ${JSON.stringify(detail)}`}`);
	passed++;
};

async function open(query) {
	const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1, serviceWorkers: 'block' });
	await context.route((u) => u.pathname.startsWith('/api/'), async (route) => {
		const path = new URL(route.request().url()).pathname.replace('/api/v1', '');
		if (path === '/models') return route.fulfill({ json: registry });
		if (path === '/settings/claude-auth') return route.fulfill({ json: { mode: 'api_key' } });
		return route.fulfill({ status: 404, json: { detail: 'not in this fixture' } });
	});
	const page = await context.newPage();
	const errors = [];
	page.on('pageerror', (e) => errors.push(e.message));
	await page.goto(`${base}${query}`);
	const row = page.getByTestId('automation-model-row');
	await row.waitFor();
	return { context, page, row, errors };
}
const shot = async (page, file) => { if (shots) await page.screenshot({ path: join(shots, `1440x900-${file}.png`), fullPage: true }); };

// 1. A plain agent: the empty option is the app default, named.
{
	const { context, page, row, errors } = await open('');
	const picker = row.getByRole('button', { name: 'App default' });
	check(await picker.isVisible(), 'empty option reads "App default"');
	check(await row.getByText('Uses Sonnet, the app default.').isVisible(), 'helper names the app default');
	check((await row.innerText()).toLowerCase().includes('agent') === false, 'no agent wording in the Model row', await row.innerText());
	await shot(page, 'app-default');

	// 2. Choose a model: the picker shows it and the helper changes.
	await picker.click();
	await page.getByRole('tab', { name: /^Claude/ }).click();
	await page.getByRole('option', { name: /Opus/ }).click();
	// The menu may stay open after a pick; the trigger is the picker's button.
	const trigger = row.locator('#automation-model');
	await row.getByText('Every run uses this model.').waitFor();
	check((await trigger.innerText()).includes('Opus'), 'picker shows the chosen model', await trigger.innerText());
	check(await row.getByText('Every run uses this model.').isVisible(), 'helper for a chosen model');
	await shot(page, 'chosen');
	check(errors.length === 0, 'no page errors', errors);
	await context.close();
}

// 3. An agent with a legacy saved model: described truthfully.
{
	const { context, page, row, errors } = await open('?agent=legacy');
	check(await row.getByRole('button', { name: 'Saved on agent (legacy)' }).isVisible(), 'legacy empty option');
	check(await row.getByText('Uses GPT-5.6, saved on this agent (legacy).').isVisible(), 'helper names the legacy model');
	await shot(page, 'legacy-agent');
	check(errors.length === 0, 'no page errors', errors);
	await context.close();
}

await browser.close();
console.log(`verify-automation-form: ${passed} checks passed`);
