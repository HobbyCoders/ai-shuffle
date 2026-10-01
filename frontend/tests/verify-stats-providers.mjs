// Real-browser check of the Stats page's "Usage by provider" card (#1614):
// every provider gets a row, unpriced cost reads "No price" (never $0),
// subscription cost is labelled as an API-price equivalent, the CSV export
// hits the per-provider endpoint, and nothing overflows at desktop or phone
// width.
//
// Run from frontend/ while the fixture is serving:
//   npx vite dev --config tests/vite.stats-providers.config.js --host 127.0.0.1 --port 5287 --strictPort
//   node tests/verify-stats-providers.mjs
// Needs browser-mcp's playwright-core (npm ci --prefix ../browser-mcp) and a
// Chrome/Chromium binary (CHROMIUM_PATH). STATS_FIXTURE_URL overrides the URL;
// SHOTS_DIR saves screenshots.
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
const base = process.env.STATS_FIXTURE_URL || 'http://127.0.0.1:5287/tests/stats-providers.html';
let passed = 0;

async function run(viewport, query = '') {
	const context = await browser.newContext({ viewport, deviceScaleFactor: 1, serviceWorkers: 'block' });
	const page = await context.newPage();
	const errors = [];
	page.on('pageerror', (e) => errors.push(e.message));
	const name = `${viewport.width}x${viewport.height}${query}`;
	const check = (cond, label, detail) => {
		assert.ok(cond, `[${name}] ${label}${detail === undefined ? '' : `: ${JSON.stringify(detail)}`}`);
		passed++;
	};
	await page.goto(base + query);
	await page.getByText('Usage by provider').waitFor();
	await page.waitForTimeout(300);
	return { context, page, errors, check, name };
}

for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
	const { context, page, errors, check, name } = await run(viewport);
	const rows = page.locator('.prov-row[data-provider]');
	check((await rows.count()) === 5, 'one row per provider', await rows.count());

	const row = (key) => page.locator(`.prov-row[data-provider="${key}"]`);
	const local = row('p-local');
	check((await local.locator('.prov-cost-val').innerText()) === 'No price', 'unpriced provider shows No price');
	check(!(await local.innerText()).includes('$0'), 'unpriced provider never shows $0', await local.innerText());
	check((await row('claude').locator('.prov-chip').innerText()) === 'Subscription', 'Claude billing chip');
	check((await row('claude').locator('.prov-cost-note').innerText()) === 'API equiv.', 'subscription cost note');
	check((await row('p-oai').locator('.prov-cost-note').innerText()) === '+2 unpriced', 'partial cost note');
	check((await row('unattributed').locator('.prov-chip').innerText()) === 'Billing unknown', 'unknown billing chip');
	check(await row('claude').locator('[data-testid="usage-estimated"]').isVisible(), 'estimated-token badge on Claude');
	check(await page.getByText('140 older turns were recorded before per-provider tracking').isVisible(), 'inferred footnote');
	check(await page.getByText('11 turns have no price').isVisible(), 'spend tile warns about unpriced turns');

	// No horizontal overflow: the page and every row fit the viewport.
	const overflow = await page.evaluate(() => {
		const scroller = document.querySelector('.st-scroll');
		const card = document.querySelector('.bcard.c4');
		const bad = [...document.querySelectorAll('.prov-row')].filter((r) => r.scrollWidth > r.clientWidth + 1).length;
		return {
			page: document.documentElement.scrollWidth - window.innerWidth,
			scroller: scroller.scrollWidth - scroller.clientWidth,
			cardRight: card.getBoundingClientRect().right - window.innerWidth,
			bad,
		};
	});
	check(overflow.page <= 0 && overflow.scroller <= 0 && overflow.cardRight <= 0 && overflow.bad === 0, 'no horizontal overflow', overflow);

	// Every row's cost cell sits inside the card on this width.
	const costInside = await page.evaluate(() => {
		const card = document.querySelector('.bcard.c4').getBoundingClientRect();
		return [...document.querySelectorAll('.prov-row[data-provider] .prov-cost')].every((c) => {
			const r = c.getBoundingClientRect();
			return r.right <= card.right + 0.5 && r.left >= card.left - 0.5;
		});
	});
	check(costInside, 'cost cells stay inside the card');

	// Nothing in a row overlaps the cost text (long names ellipsize instead),
	// and the cost cell is wide enough for its text.
	const overlaps = await page.evaluate(() =>
		[...document.querySelectorAll('.prov-row[data-provider]')].flatMap((row) => {
			const cell = row.querySelector('.prov-cost').getBoundingClientRect();
			const cost = row.querySelector('.prov-cost-val').getBoundingClientRect();
			const squeezed = cell.width + 0.5 < cost.width ? [`${row.dataset.provider}:cost-cell-too-narrow`] : [];
			return squeezed.concat([...row.querySelectorAll('.prov-label, .prov-chip, .prov-num')]
				.filter((el) => {
					const r = el.getBoundingClientRect();
					return r.width > 0 && r.right > cost.left + 0.5 && r.left < cost.right && r.bottom > cost.top && r.top < cost.bottom;
				})
				.map((el) => `${row.dataset.provider}:${el.className}`));
		}),
	);
	check(overlaps.length === 0, 'no cell overlaps the cost', overlaps);

	if (viewport.width < 540) {
		check(!(await page.locator('.prov-head').isVisible()), 'column header hidden on phone');
		check(await row('claude').locator('.prov-mlabel').first().isVisible(), 'inline labels on phone');
	} else {
		check(await page.locator('.prov-head').isVisible(), 'column header on desktop');
	}

	// Export: the button asks the per-provider CSV endpoint for the shown range.
	await page.getByRole('button', { name: 'Export usage by provider as CSV' }).click();
	await page.waitForTimeout(300);
	const requests = await page.evaluate(() => window.__statsRequests);
	check(requests.some((r) => r.startsWith('/api/v1/analytics/export?start_date=')), 'export requested', requests);
	check(requests.some((r) => r.startsWith('/api/v1/analytics/providers?start_date=')), 'providers requested');

	// Switching range reloads the breakdown for the new range.
	const before = requests.filter((r) => r.includes('/analytics/providers')).length;
	await page.getByRole('button', { name: '7 days' }).click();
	await page.getByText('Usage by provider').waitFor();
	await page.waitForTimeout(300);
	const after = (await page.evaluate(() => window.__statsRequests)).filter((r) => r.includes('/analytics/providers')).length;
	check(after === before + 1, 'range change refetches providers', { before, after });

	if (process.env.SHOTS_DIR) {
		await page.screenshot({ path: join(process.env.SHOTS_DIR, `stats-${viewport.width}.png`) });
		await page.locator('.bcard.c4').screenshot({ path: join(process.env.SHOTS_DIR, `stats-providers-${viewport.width}.png`) });
	}
	check(errors.length === 0, 'no page errors', errors);
	console.log(`ok ${name}`);
	await context.close();
}

for (const [query, text] of [['?providers=fail', 'Provider breakdown unavailable'], ['?providers=empty', 'No usage in this range']]) {
	const { context, page, check, errors } = await run({ width: 1440, height: 900 }, query);
	check(await page.locator('.bcard.c4').getByText(text).isVisible(), `state ${query}`);
	// The rest of the page still renders when the breakdown fails.
	check(await page.getByText('Top sessions by cost').isVisible(), `rest of page renders ${query}`);
	check(errors.length === 0, 'no page errors', errors);
	await context.close();
}

await browser.close();
console.log(`${passed} checks passed`);
