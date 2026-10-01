// Real-browser check of the usage popover rows (#1607) and the loop guard
// explainer (#1613) at desktop and phone widths.
//
// Run from frontend/ while the fixture is serving:
//   npx vite dev --config tests/vite.usage-rows.config.js --host 127.0.0.1 --port 5291 --strictPort
//   node tests/verify-usage-rows.mjs [screenshot-dir]
// Needs browser-mcp's playwright-core (npm ci --prefix ../browser-mcp) and a
// Chrome/Chromium binary (CHROMIUM_PATH). USAGE_ROWS_FIXTURE_URL overrides the URL.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { join } from 'node:path';
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
const base = process.env.USAGE_ROWS_FIXTURE_URL || 'http://127.0.0.1:5291/tests/usage-rows.html';
const shots = process.argv[2];
let passed = 0;

async function run(viewport, touch) {
	const name = `${viewport.width}x${viewport.height}`;
	const context = await browser.newContext({ viewport, isMobile: touch, hasTouch: touch, deviceScaleFactor: 1, serviceWorkers: 'block' });
	const page = await context.newPage();
	const errors = [];
	page.on('pageerror', (e) => errors.push(e.message));
	const check = (condition, label, detail) => {
		assert.ok(condition, `[${name}] ${label}${detail === undefined ? '' : `: ${JSON.stringify(detail)}`}`);
		passed++;
	};
	const press = async (locator) => { await (touch ? locator.tap() : locator.click()); await page.waitForTimeout(120); };
	const noHorizontalOverflow = () => page.evaluate(() => {
		const vw = document.documentElement.clientWidth;
		const out = [];
		document.querySelectorAll('.usage-popover *, .guard-panel *').forEach((e) => {
			const b = e.getBoundingClientRect();
			if (b.width && (b.right > vw + 0.5 || b.left < -0.5)) out.push(`${e.tagName}.${e.className}`.slice(0, 60));
		});
		return out;
	});

	// ── Usage popover ──
	await page.goto(`${base}?view=usage`);
	await press(page.locator('.usage-indicator'));
	const popover = page.locator('.usage-popover');
	check(await popover.isVisible(), 'popover opens');
	const claude = page.getByRole('button', { name: /Plan usage limits/ });
	const claudeText = (await claude.textContent()).replace(/\s+/g, ' ');
	check(/Last reported 42m ago/.test(claudeText), 'Claude header shows Last reported with its age', claudeText);
	check(!(await popover.textContent()).includes('not live'), 'no "not live" text in the popover');
	check((await page.locator('.usage-as-of').count()) === 2, 'freshness shown once per stale row (Claude, Router)');
	const router = page.getByRole('button', { name: /^Router usage/ });
	const routerText = (await router.textContent()).replace(/\s+/g, ' ');
	check(/Last reported 25m ago/.test(routerText), 'stale provider header shows Last reported with its age', routerText);
	check((await page.locator('.usage-next-check', { hasText: /^Next check in/ }).count()) === 1, 'stale provider rows keep the next check');
	check(!(await popover.textContent()).includes('Updated '), 'no duplicate "Updated" line');

	const chatgpt = page.getByRole('button', { name: /^OpenAI usage/ }).first();
	check((await page.locator('.usage-credits', { hasText: '12.5 extra credits available' }).count()) === 1, 'ChatGPT extra credits in expanded rows');
	const openai = page.getByRole('button', { name: /^OpenAI usage/ }).nth(1);
	check((await openai.textContent()).includes('Credit balance not available for this key'), 'OpenAI API row says the balance is not available');
	check((await page.locator('a[href*="platform.openai.com/settings/organization/billing"]').count()) === 0, 'no Platform billing link');
	check((await page.getByRole('button', { name: /^fal\.ai usage/ }).count()) === 1, 'fal.ai row present');
	check((await popover.textContent()).includes('$18.11 left'), 'fal.ai balance shown');
	check((await noHorizontalOverflow()).length === 0, 'expanded popover fits the viewport', await noHorizontalOverflow());
	if (shots) await page.screenshot({ path: join(shots, `usage-expanded-${name}.png`) });

	await press(chatgpt);
	const collapsed = (await page.getByRole('button', { name: /^OpenAI usage — expand/ }).first().textContent()).replace(/\s+/g, ' ');
	check(collapsed.includes('70% left') && !collapsed.includes('extra credits'), 'collapsed ChatGPT header shows the window, not credits', collapsed);
	check((await page.locator('.usage-credits', { hasText: 'extra credits' }).count()) === 0, 'credits hidden while collapsed');
	await press(claude);
	const claudeCollapsed = (await page.getByRole('button', { name: /Plan usage limits — expand/ }).textContent()).replace(/\s+/g, ' ');
	check(/Last reported 42m ago/.test(claudeCollapsed), 'collapsed Claude header keeps the freshness', claudeCollapsed);
	await press(router);
	const routerCollapsed = (await page.getByRole('button', { name: /^Router usage — expand/ }).textContent()).replace(/\s+/g, ' ');
	check(/Last reported 25m ago/.test(routerCollapsed), 'collapsed provider header keeps the freshness', routerCollapsed);
	if (shots) await page.screenshot({ path: join(shots, `usage-collapsed-${name}.png`) });
	// Restore the shared expansion state for the next viewport.
	await press(page.getByRole('button', { name: /^OpenAI usage — expand/ }).first());
	await press(page.getByRole('button', { name: /Plan usage limits — expand/ }));
	await press(page.getByRole('button', { name: /^Router usage — expand/ }));

	// ── Settings panels ──
	await page.goto(`${base}?view=settings`);
	const explainer = page.getByTestId('loop-guard-explainer');
	await explainer.waitFor();
	check(!(await explainer.evaluate((d) => d.open)), 'explainer starts collapsed');
	await press(explainer.locator('summary'));
	check(await explainer.evaluate((d) => d.open), 'explainer opens');
	const text = (await explainer.textContent()).replace(/\s+/g, ' ');
	check(text.includes('Defaults: 12 per session, 60 per worker.'), 'explainer states the defaults');
	check(text.includes('Not covered:'), 'explainer states what is not covered');
	check((await page.getByTestId('loop-guard-held').textContent()).includes('Confirm starts the turn now'), 'held list says what Confirm does');
	check((await page.getByText('fal-abc...wxyz').count()) === 1, 'fal.ai key card shows the saved key');
	check((await page.getByText(/Admin API key|billing access/i).count()) === 0, 'no fal.ai Admin billing UI');
	check((await noHorizontalOverflow()).length === 0, 'settings panel fits the viewport', await noHorizontalOverflow());
	if (shots) await page.screenshot({ path: join(shots, `settings-${name}.png`), fullPage: true });

	check(errors.length === 0, 'no page errors', errors);
	await context.close();
}

try {
	await run({ width: 1440, height: 900 }, false);
	await run({ width: 390, height: 844 }, true);
	console.log(`usage rows: ${passed} checks passed`);
} finally {
	await browser.close();
}
