// Real-browser check of "Send report to HobbyCoders support": the error banner
// entry point, the preview of exactly what is sent, the known/fixed notice,
// the AI acknowledgement, a running and a finished diagnosis, sending, and the
// error states. Every /api/v1 request is answered here; nothing reaches the
// hub, an AI provider, GitHub or email.
//
// Run from frontend/ while the fixture is serving:
//   npx vite dev --config tests/vite.support-report.config.js --host 127.0.0.1 --port 5249 --strictPort
//   node tests/verify-support-report.mjs
// Needs browser-mcp's playwright-core (npm ci --ignore-scripts --prefix ../browser-mcp)
// and a Chrome/Chromium binary (CHROMIUM_PATH). SUPPORT_REPORT_FIXTURE_URL
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
const url = process.env.SUPPORT_REPORT_FIXTURE_URL || 'http://127.0.0.1:5249/tests/support-report.html';
const shots = process.env.SCREENSHOT_DIR;

const MODELS = {
	default: 'sonnet',
	legacy_map: {},
	models: [{ value: 'sonnet', label: 'Sonnet', family: 'sonnet' }],
	claude: { auth_kind: 'subscription', plan_type: 'max' },
	providers: [{
		id: 'p-openai', name: 'OpenAI', provider_type: 'openai', default_model: 'gpt-6.1',
		models: [
			{ id: 'gpt-6.1', label: 'GPT-6.1', effort_levels: [], effort_metadata_version: 1 },
			{ id: 'gpt-6.1-mini', label: 'GPT-6.1 mini', effort_levels: [], effort_metadata_version: 1 },
		],
	}],
};
const caps = (over = {}) => ({
	basic_report: true, ai_diagnosis: true, signed_in: true, default_model: 'prov:p-openai:gpt-6.1',
	fix_with_ai: false, fix_with_ai_reason: null,
	diagnosis_caps: { max_turns: 1, timeout_seconds: 180, prompt_max_chars: 60000, tools: [], cost_cap: null },
	...over,
});
const BODY = [
	'AI Shuffle support report (basic)',
	'App version: 0.9.1',
	'Platform: docker linux x86_64',
	'',
	'## Error',
	'Source: ws',
	'Message: API error: HTTP 500',
	'',
	'## Recent errors',
	...Array.from({ length: 14 }, (_, i) => `- 2026-10-03T10:0${i % 10}:00 ERROR backend (x1): Upstream request failed with token=*** attempt ${i + 1}`),
	'',
	'## Log excerpt',
	'2026-10-03 10:00:00 - app.api.sessions - ERROR - IndexError: list index out of range in ~/app/api/sessions.py',
].join('\n');
const preview = (over = {}) => ({
	report_id: 'basic-1', kind: 'basic', status: 'draft', fingerprint: '5d41402abc4b2a76b9719d911017c592',
	title: 'API error: HTTP 500', app_version: '0.9.1', platform: 'docker linux x86_64', diagnosis: null,
	body: BODY, body_chars: BODY.length, redactions: 14, user_note: null, sent: null,
	created_at: '2026-10-03T10:05:00+00:00', expires_at: '2026-10-03T11:05:00+00:00', ...over,
});
const withNote = (p, note) => ({ ...p, user_note: note || null, body: note ? `${p.body}\n\n## User note\n${note}` : p.body });
const issue = (over = {}) => ({ status: 'new', fixed_in: null, note: null, reported: false, update_available: false, ...over });
const AI_TEXT = 'The worker could not reach the model provider, so the request ended with HTTP 500.\nThe log shows the same failure 14 times in the last hour.';
const aiPreview = (note) => withNote(preview({
	report_id: 'ai-1', kind: 'ai', body: BODY.replace('(basic)', '(ai)') + `\n\n## AI diagnosis (OpenAI / gpt-6.1-mini)\n${AI_TEXT}`,
	diagnosis: { model: 'OpenAI / gpt-6.1-mini', provider: 'OpenAI', model_id: 'gpt-6.1-mini', text: AI_TEXT, workaround: '- Reload the page.\n- Check the provider is signed in under Settings.' },
}), note);

let passed = 0;

async function harness(viewport, touch) {
	const name = `${viewport.width}x${viewport.height}`;
	const check = (condition, label, detail) => {
		assert.ok(condition, `[${name}] ${label}${detail === undefined ? '' : `: ${JSON.stringify(detail)}`}`);
		passed++;
	};
	const shot = async (page, file) => { if (shots) await page.screenshot({ path: join(shots, `${name}-${file}.png`) }); };

	async function open(scenario) {
		const context = await browser.newContext({ viewport, isMobile: touch, hasTouch: touch, deviceScaleFactor: 1, serviceWorkers: 'block' });
		const state = { calls: [], note: null, diagnosis: 'running', ...scenario };
		await context.route((u) => u.pathname.startsWith('/api/'), async (route) => {
			const req = route.request();
			const path = new URL(req.url()).pathname.replace('/api/v1', '');
			const body = req.postData() ? req.postDataJSON() : null;
			state.calls.push({ method: req.method(), path, body });
			if (path === '/models') return route.fulfill({ json: MODELS });
			if (path === '/support/capabilities') return route.fulfill({ json: state.caps });
			if (path === '/support/reports' && req.method() === 'POST') return route.fulfill({ json: withNote(preview(), body.user_note) });
			if (/^\/support\/reports\/[^/]+\/note$/.test(path)) {
				if (path.includes('ai-1')) {
					state.aiNote = body.user_note;
					return route.fulfill({ json: aiPreview(state.aiNote) });
				}
				state.note = body.user_note;
				return route.fulfill({ json: withNote(preview(), state.note) });
			}
			if (path === '/support/check') return route.fulfill({ json: state.check });
			// Like the server, the AI draft copies the basic draft's note when the run starts.
			if (path === '/support/diagnoses') state.aiNote = state.note;
			if (path === '/support/diagnoses') return route.fulfill({ status: 202, json: { run_id: 'run-1', status: 'running', base_report_id: 'basic-1', report_id: null, error: null, model: 'OpenAI / gpt-6.1-mini', started_at: '2026-10-03T10:06:00+00:00', finished_at: null } });
			if (path === '/support/diagnoses/run-1') {
				const run = { run_id: 'run-1', base_report_id: 'basic-1', model: 'OpenAI / gpt-6.1-mini', started_at: '2026-10-03T10:06:00+00:00' };
				if (state.diagnosis === 'done') return route.fulfill({ json: { ...run, status: 'done', report_id: 'ai-1', error: null, report: aiPreview(state.aiNote) } });
				if (state.diagnosis === 'failed') return route.fulfill({ json: { ...run, status: 'failed', report_id: null, error: 'The model is not signed in.' } });
				return route.fulfill({ json: { ...run, status: 'running', report_id: null, error: null } });
			}
			if (/^\/support\/reports\/[^/]+\/send$/.test(path)) return route.fulfill({ json: state.send });
			return route.fulfill({ status: 404, json: { detail: 'not faked' } });
		});
		const page = await context.newPage();
		const errors = [];
		page.on('pageerror', (e) => errors.push(e.message));
		await page.goto(url);
		await page.waitForFunction(() => window.__ready === true);
		return { context, page, state, errors };
	}

	const dialog = (page) => page.getByRole('dialog');
	async function fits(page, label) {
		const panel = page.locator('[role="dialog"] .bg-card').first();
		const box = await panel.boundingBox();
		check(box && box.x >= 0 && box.y >= 0 && box.x + box.width <= viewport.width + 0.5 && box.y + box.height <= viewport.height + 0.5, `${label}: dialog within viewport`, box);
		const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
		check(overflow <= 0, `${label}: no horizontal page overflow`, overflow);
	}
	async function openFromBanner(page) {
		await page.getByRole('button', { name: 'Send report to support' }).click();
		await dialog(page).getByLabel('Report text').waitFor();
	}

	// 1. Customer install: banner offers only the report; preview, fixed notice,
	//    AI acknowledgement, running, done, note, send.
	let run = await open({
		caps: caps(),
		check: { state: 'ok', fingerprint: 'x', known_issue: issue({ status: 'fixed', fixed_in: '0.9.2', reported: true, update_available: true }) },
		send: { state: 'ok', report_id: 'ai-1', hub_report_id: '0e7c55aa91f04c2d', sent_at: '2026-10-03T10:08:00+00:00', delivered: { email: 'sent', github: 'created' }, known_issue: issue({ reported: true }) },
	});
	let { page, state } = run;
	check(await page.getByRole('button', { name: 'Send report to support' }).isVisible(), 'banner offers Send report to support');
	check(await page.getByRole('button', { name: 'Fix with AI' }).count() === 0, 'banner hides Fix with AI on a customer install');
	await shot(page, '0-banner');
	await openFromBanner(page);
	const bodyText = await dialog(page).getByLabel('Report text').innerText();
	check(bodyText.includes('AI Shuffle support report (basic)') && bodyText.includes('token=***'), 'preview shows the exact body', bodyText.slice(0, 80));
	check(await dialog(page).getByText('14 secrets hidden as ***').isVisible(), 'redaction count shown');
	await dialog(page).getByText('Fixed in version 0.9.2').waitFor();
	check(await dialog(page).getByRole('button', { name: 'Open updates' }).isVisible(), 'fixed notice points to updates');
	check(!state.calls.some((c) => c.path.endsWith('/send')), 'nothing sent on open');
	check(state.calls.find((c) => c.path === '/support/check')?.body?.report_id === 'basic-1', 'check uses the draft id');
	const pre = dialog(page).getByLabel('Report text');
	const scroll = await pre.evaluate((el) => ({ sh: el.scrollHeight, ch: el.clientHeight }));
	check(scroll.sh > scroll.ch, 'long preview scrolls inside its box', scroll);
	check(await pre.evaluate((el) => getComputedStyle(el).fontVariantLigatures) === 'none', 'preview shows *** without ligatures');
	await fits(page, 'preview');
	await shot(page, '1-preview-fixed');

	// AI step: cost statement, default model, picker, acknowledgement.
	await dialog(page).getByRole('button', { name: 'Add AI diagnosis' }).click();
	check(await dialog(page).getByText(/counts against your AI usage or subscription/).isVisible(), 'AI cost is stated before starting');
	const trigger = dialog(page).locator('#sr-model');
	check((await trigger.innerText()).includes('GPT-6.1'), 'default model is shown', await trigger.innerText());
	const start = dialog(page).getByRole('button', { name: 'Start diagnosis' });
	check(await start.isDisabled(), 'Start is disabled before the acknowledgement');
	await trigger.click();
	const option = page.getByRole('option', { name: /GPT-6\.1 mini/ });
	await option.waitFor();
	const panelBox = await page.locator('.mpk-panel').boundingBox();
	check(panelBox && panelBox.x >= 0 && panelBox.x + panelBox.width <= viewport.width + 0.5, 'model picker panel within viewport', panelBox);
	await shot(page, '2-model-picker');
	await option.click();
	await page.keyboard.press('Escape');
	await page.locator('.mpk-panel').waitFor({ state: 'detached' });
	check(await dialog(page).count() === 1, 'Escape closes only the model picker');
	check((await trigger.innerText()).includes('GPT-6.1 mini'), 'picked model shown');
	await dialog(page).getByLabel('I understand this uses my AI usage').check();
	check(await dialog(page).getByRole('button', { name: 'Start diagnosis' }).isEnabled(), 'acknowledgement enables Start');
	await fits(page, 'ai confirm');
	await shot(page, '3-ai-acknowledge');
	await dialog(page).getByRole('button', { name: 'Start diagnosis' }).click();
	await dialog(page).getByText(/Diagnosing with OpenAI \/ gpt-6\.1-mini/).waitFor();
	const started = state.calls.find((c) => c.path === '/support/diagnoses');
	check(started?.body?.acknowledge_ai_usage === true && started.body.model === 'prov:p-openai:gpt-6.1-mini' && started.body.report_id === 'basic-1', 'diagnosis request carries model and acknowledgement', started?.body);
	check(await page.getByRole('button', { name: /^Send/ }).isDisabled(), 'Send waits while the diagnosis runs');
	await page.waitForTimeout(1600);
	await fits(page, 'running');
	await shot(page, '4-ai-running');

	// Note typed while it runs, then the run finishes.
	await dialog(page).getByPlaceholder('What were you doing?').fill('I opened the session list.');
	state.diagnosis = 'done';
	await dialog(page).getByText('What you can try').waitFor({ timeout: 8000 });
	check(await dialog(page).locator('.sr-diagnosis').first().getByText(/could not reach the model provider/).isVisible(), 'diagnosis text shown');
	check(await dialog(page).getByText('By OpenAI / gpt-6.1-mini').isVisible(), 'diagnosis names its model');
	await page.waitForFunction(() => document.querySelector('[aria-label="Report text"]')?.textContent?.includes('I opened the session list.'), null, { timeout: 5000 });
	const aiBody = await dialog(page).getByLabel('Report text').innerText();
	check(aiBody.includes('(ai)') && aiBody.includes('## AI diagnosis'), 'preview switched to the AI report');
	check(state.calls.some((c) => c.path === '/support/reports/ai-1/note' && c.body.user_note === 'I opened the session list.'), 'note saved into the AI draft');
	check((await page.getByRole('button', { name: /^Send/ }).innerText()).includes('Send with diagnosis'), 'Send names the AI report');
	await fits(page, 'done');
	await shot(page, '5-ai-done');

	await page.getByRole('button', { name: /^Send/ }).click();
	await dialog(page).getByText('Sent to HobbyCoders support').waitFor();
	const sends = state.calls.filter((c) => c.path.endsWith('/send'));
	check(sends.length === 1 && sends[0].path === '/support/reports/ai-1/send' && sends[0].body.confirm === true, 'one confirmed send of the AI draft', sends);
	check(await dialog(page).getByText('0e7c55aa91f0').isVisible(), 'reference shown');
	check(await dialog(page).getByRole('button', { name: 'Done' }).isVisible(), 'Done closes');
	await page.waitForTimeout(500); // let the check mark finish its pop
	await fits(page, 'sent');
	await shot(page, '6-sent');
	check(run.errors.length === 0, 'no page errors', run.errors);
	await run.context.close();

	// 2. Source checkout: both actions; not signed in blocks Send.
	run = await open({ caps: caps({ fix_with_ai: true, fix_with_ai_reason: 'source_checkout', signed_in: false }), check: { state: 'not_signed_in', fingerprint: 'x', known_issue: null, message: 'Sign this worker in to a Shuffle account to contact HobbyCoders support.' } });
	({ page, state } = run);
	check(await page.getByRole('button', { name: 'Fix with AI' }).isVisible(), 'banner keeps Fix with AI on a source checkout');
	await shot(page, '7-banner-dev');
	await openFromBanner(page);
	await dialog(page).getByText('Not signed in').waitFor();
	check(await dialog(page).getByText(/Sign this worker in to a Shuffle account/).isVisible(), 'not-signed-in says how');
	check(await dialog(page).getByRole('button', { name: 'Open Shuffle account' }).isVisible(), 'offers the account screen');
	check(await page.getByRole('button', { name: /^Send/ }).isDisabled(), 'Send disabled while signed out');
	await fits(page, 'not signed in');
	await shot(page, '8-not-signed-in');
	state.check = { state: 'ok', fingerprint: 'x', known_issue: issue() };
	await dialog(page).getByRole('button', { name: 'Check again' }).click();
	await page.waitForFunction(() => ![...document.querySelectorAll('button')].find((b) => b.textContent?.trim() === 'Send report')?.disabled);
	check(await dialog(page).getByText('Not signed in').count() === 0, 'Check again after signing in clears the notice');
	check(run.errors.length === 0, 'no page errors', run.errors);
	await run.context.close();

	// 3. Known issue; rate limited, then stored-only delivery.
	run = await open({
		caps: caps(),
		check: { state: 'ok', fingerprint: 'x', known_issue: issue({ status: 'known', reported: true, note: 'We are fixing this in the next release.' }) },
		send: { state: 'rate_limited', report_id: 'basic-1', message: 'Too many reports from this worker or account. Try again later.', retry_after: 1200 },
	});
	({ page, state } = run);
	await openFromBanner(page);
	await dialog(page).getByText('Known issue').waitFor();
	check(await dialog(page).getByText('We are fixing this in the next release.').isVisible(), 'known-issue note shown');
	await page.getByRole('button', { name: /^Send/ }).click();
	await dialog(page).getByText('Too many reports', { exact: true }).waitFor();
	check(await dialog(page).getByText(/Try again in 20 minutes\./).isVisible(), 'rate limit says when to retry');
	check(await dialog(page).getByLabel('Report text').isVisible(), 'draft kept after a refusal');
	check(await dialog(page).getByRole('button', { name: 'Check again' }).count() === 0, 'a send refusal does not offer the known-issue check');
	await fits(page, 'rate limited');
	await shot(page, '9-rate-limited');
	state.send = { state: 'ok', report_id: 'basic-1', hub_report_id: 'aa11', delivered: { email: 'not_configured', github: 'deferred' }, known_issue: null };
	await page.getByRole('button', { name: /^Send/ }).click();
	await dialog(page).getByText('Saved by HobbyCoders').waitFor();
	check(await dialog(page).getByText('Sent to HobbyCoders support').count() === 0, 'stored-only report is not called delivered');
	await shot(page, '10-stored-only');
	check(run.errors.length === 0, 'no page errors', run.errors);
	await run.context.close();
}

try {
	await harness({ width: 1280, height: 800 }, false);
	await harness({ width: 390, height: 844 }, true);
	console.log(`support report: ${passed} checks passed`);
} finally {
	await browser.close();
}
