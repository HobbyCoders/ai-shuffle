// Settings > Memory > Memory health against synthetic answers (never the
// owner's memory): healthy stays quiet, a loss shows on the Memory list's
// health row without opening it and never in the sidebar's "Needs you" lane
// (the owner cannot act on it; the worker repairs by itself), a late answer
// moves nothing in the detail pane, and a store never checked says so (#1042).
// It was a row on Settings > System until Settings was reorganized.
import { runJourney } from './runner.mjs';
import { expect, gotoApp, showSidebar } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const MINUTE = 60;
const now = () => Math.floor(Date.now() / 1000);
const LOSS = 'Automatic cleanup has recorded 3 permanent deletions of older facts (3 at the archive limit; 0 on expiry). Raise the archive limit to keep more.';
// The shapes app/core/fluid_memory.py owner_health returns.
const healthy = () => ({
	state: 'healthy',
	checked_at: now() - 12 * MINUTE,
	signals: [
		{ id: 'capacity', label: 'Capacity', state: 'healthy', reason: 'Room for 1,200 more facts.' },
		{ id: 'deletions', label: 'Past deletions', state: 'healthy', reason: 'Two facts were deleted on expiry last month.', note: true }
	],
	repairs: []
});
const attention = (checkedAt = now() - 12 * MINUTE) => ({
	state: 'attention',
	checked_at: checkedAt,
	signals: [
		{ id: 'capacity', label: 'Capacity', state: 'attention', reason: LOSS },
		{ id: 'index', label: 'Search index', state: 'attention', reason: 'The search index is 40 facts behind.' },
		{ id: 'backup', label: 'Backup', state: 'healthy', reason: 'Backed up 2h ago.' }
	],
	repairs: []
});

const HEALTH = /\/api\/v1\/memory\/health(\?|$)/;
const row = (page) => page.locator('[data-setting-anchor="memory.health"]').filter({ visible: true }).first();
const hint = (page) => row(page).locator('.st-rh');
// The health item in the Memory list, beside Learned guidance.
const listRow = (page) => page.locator('[data-settings-item="health"]').filter({ visible: true }).first();
const details = (page) => page.locator('.mh-list').filter({ visible: true });
// The page carries no warning notice of its own: the row's value says it.
const pageNotice = (page) => page.locator('.settings-viewport .st-banner').filter({ visible: true });
const lane = (page) => page.locator('[data-group="needs-you"]').filter({ visible: true });
// The pane's title, above the row: it must not move either.
const title = (page) => page.getByRole('heading', { name: 'Memory health', exact: true }).filter({ visible: true }).first();
const top = (locator) => locator.evaluate((el) => Math.round(el.getBoundingClientRect().top));

/**
 * Answer the health check with `payload` (the app asks only as Settings >
 * Memory opens). With `held`, the answer waits until
 * `page.memoryHealth.release()` (a worker slow to answer). `answer(next)`
 * changes what later requests get.
 */
async function answerHealth(page, payload, { held = false } = {}) {
	let release;
	const gate = held ? new Promise((done) => (release = done)) : Promise.resolve();
	page.memoryHealth = { hits: 0, payload, release: () => release?.(), answer: (next) => (page.memoryHealth.payload = next) };
	await page.route(HEALTH, async (route) => {
		if (route.request().method() !== 'GET') return route.continue();
		page.memoryHealth.hits++;
		await gate;
		await route.fulfill({ json: page.memoryHealth.payload }).catch(() => {});
	});
}

/** Settings > Memory, the list only. */
async function openMemoryList(page, status, variant) {
	await gotoApp(page, status);
	if (variant.viewport === 'phone') await showSidebar(page);
	await page.getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('button', { name: 'Settings', exact: true }).click();
	await page.getByRole('navigation', { name: 'Settings', exact: true }).getByRole('button', { name: 'Memory', exact: true }).filter({ visible: true }).first().click();
	await listRow(page).waitFor({ timeout: 15_000 });
}

/** Settings > Memory > Memory health, the detail pane. */
async function openHealth(page, status, variant) {
	await openMemoryList(page, status, variant);
	await listRow(page).click();
	await row(page).waitFor({ timeout: 15_000 });
}

/** The list row's text, waited on until it has `text`. */
const listRowSays = (page, text) =>
	page.waitForFunction(
		(want) => [...document.querySelectorAll('[data-settings-item="health"]')].some((el) => el.checkVisibility() && el.textContent.includes(want)),
		text,
		{ timeout: 15_000 }
	);

const valueIs = (page, text) =>
	page.waitForFunction(
		(want) => [...document.querySelectorAll('[data-testid="memory-health"]')].some((el) => el.checkVisibility() && el.textContent.trim() === want),
		text,
		{ timeout: 15_000 }
	);

const positions = async (page) => ({ row: await top(row(page)), title: await top(title(page)) });

const LOGS = /\/api\/v1\/logs(\?|$)/;
const LOG_LINES = 120;

/**
 * Answer Settings > Logs with a fixed list of LOG_LINES lines (the shape
 * app/api/logs.py get_logs returns), so the page is long whatever the worker
 * has logged. The lines are built once: the 10s poll gets the same lines and
 * ids, so it never replaces or shortens the list.
 */
async function answerLogs(page) {
	const start = Date.now();
	const lines = Array.from({ length: LOG_LINES }, (_, i) => {
		const timestamp = new Date(start - i * 1000).toISOString();
		const level = i % 7 === 3 ? 'WARNING' : 'INFO';
		const logger_name = `app.journey.fixture${i % 4}`;
		return { timestamp, level, logger_name, message: `${timestamp} - ${logger_name} - ${level} - Journey fixture log line ${LOG_LINES - i}` };
	});
	const payload = { lines, total: LOG_LINES, returned: LOG_LINES, levels: ['INFO', 'WARNING'] };
	await page.route(LOGS, async (route) => {
		if (route.request().method() !== 'GET') return route.continue();
		await route.fulfill({ json: payload }).catch(() => {});
	});
}

/**
 * Bring the row into view the way a reader scrolls to it, then let input
 * grace pass so a later move counts. Returns where the row and title sit.
 */
async function scrollToRow(page) {
	await row(page).evaluate((el) => el.scrollIntoView({ block: 'center' }));
	await page.waitForTimeout(800);
	return positions(page);
}

/**
 * An attention answer that lands four seconds late, after the reader has
 * scrolled the row into view. The row's value and hint change and the
 * details list appears below it: there is no warning notice, so nothing at or
 * above the row moves.
 */
function lateAttention(viewport) {
	return {
		name: 'attention-late-after-scroll',
		viewports: [viewport],
		throttles: ['fast', 'slow'],
		async setup({ page, status, variant }) {
			await answerHealth(page, attention(), { held: true });
			await openHealth(page, status, variant);
			await valueIs(page, 'Loading');
		},
		async act({ page, mark }) {
			await mark('the reader scrolls Memory health into view while it loads');
			const before = await scrollToRow(page);
			await page.waitForTimeout(3200);
			await mark('the attention answer lands late');
			page.memoryHealth.release();
			await valueIs(page, '2 need attention');
			await page.waitForTimeout(1500);
			const after = await positions(page);
			expect(after.row === before.row && after.title === before.title, `nothing moved: row ${before.row}→${after.row}, title ${before.title}→${after.title}`);
			expect((await hint(page).textContent()) === 'Checked 12m ago', 'the hint says when it checked');
			expect((await pageNotice(page).count()) === 0, 'no notice in the page flow');
		}
	};
}

/**
 * The same late answer after arriving by Settings search from a scrolled Logs
 * page. On System, Chromium's scroll anchoring once scrolled the page by an
 * old notice's height when it landed, moving the row the search took the
 * reader to up by 67px; the row now sits in the Memory detail pane and must
 * stay put there too. Logs gets a fixed answer, so the scrolled start does not
 * depend on how much the worker has logged.
 */
const searchArrival = {
	name: 'attention-late-after-search',
	viewports: ['desktop'],
	throttles: ['fast'],
	async setup({ page, status }) {
		await answerHealth(page, attention(), { held: true });
		await answerLogs(page);
		await gotoApp(page, status);
		await page.getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('button', { name: 'Settings', exact: true }).click();
		await page.getByRole('navigation', { name: 'Settings', exact: true }).getByRole('button', { name: 'Logs', exact: true }).filter({ visible: true }).first().click();
		await page.getByText(`Journey fixture log line ${LOG_LINES}`, { exact: false }).filter({ visible: true }).first().waitFor({ timeout: 15_000 });
		// The reader scrolls down the log, so the search reveal scrolls up.
		await page.waitForFunction(
			() => {
				const s = document.querySelector('.settings-viewport');
				if (!s || s.scrollHeight - s.clientHeight < 1200) return false;
				s.scrollTop = s.scrollHeight;
				return true;
			},
			null,
			{ timeout: 15_000 }
		);
		await page.waitForTimeout(800);
		const scrolled = await page.evaluate(() => document.querySelector('.settings-viewport')?.scrollTop ?? 0);
		expect(scrolled >= 1200, `the reader starts far down the log (scrollTop ${scrolled})`);
	},
	async act({ page, mark }) {
		await mark('the reader follows the search result to Memory health while it loads');
		const search = page.locator('input[placeholder="Search settings"]').filter({ visible: true }).first();
		await search.fill('memory health');
		await page.waitForTimeout(400);
		await search.press('Enter');
		await row(page).waitFor({ timeout: 15_000 });
		await valueIs(page, 'Loading');
		await page.waitForTimeout(1500);
		const before = await positions(page);
		await mark('the attention answer lands late');
		page.memoryHealth.release();
		await valueIs(page, '2 need attention');
		await page.waitForTimeout(1500);
		const after = await positions(page);
		expect(after.row === before.row, `the row the search took the reader to stayed put (${before.row}→${after.row})`);
		expect((await pageNotice(page).count()) === 0, 'no notice in the page flow');
	}
};

const variants = { viewports: ['desktop', 'phone'], throttles: ['fast'] };

await runJourney({
	name: 'settings-memory-health',
	// The service worker would answer the health request before page.route sees it.
	browser: async () => ({ context: { serviceWorkers: 'block' } }),
	allow: [
		...DEV_INSTANCE_NOISE
	],
	cases: [
		{
			// Arrives late, after the reader has scrolled to the row: the row keeps
			// the shape it had while loading and nothing below it moves.
			name: 'healthy-stays-quiet',
			viewports: ['desktop', 'phone'],
			throttles: ['fast'],
			async setup({ page, status, variant }) {
				await answerHealth(page, healthy(), { held: true });
				await openHealth(page, status, variant);
				await valueIs(page, 'Loading');
			},
			async act({ page, mark }) {
				await mark('the reader scrolls Memory health into view while it loads');
				const before = await scrollToRow(page);
				await mark('a healthy answer with a note lands late');
				page.memoryHealth.release();
				await valueIs(page, 'Healthy, 1 note');
				await page.waitForTimeout(1000);
				const after = await positions(page);
				expect(after.row === before.row && after.title === before.title, `nothing moved: row ${before.row}→${after.row}, title ${before.title}→${after.title}`);
				expect((await hint(page).textContent()) === 'Checked 12m ago', `the hint says when it checked: ${await hint(page).textContent()}`);
				expect((await page.locator('.st-banner').filter({ visible: true }).count()) === 0, 'healthy shows no notice');
				expect((await page.locator('[data-needs-item]').count()) === 0, 'healthy puts nothing in Needs you');
			}
		},
		{
			name: 'attention-on-time',
			...variants,
			async setup({ page, status, variant }) {
				await answerHealth(page, attention());
				await openMemoryList(page, status, variant);
			},
			async act({ page, mark }) {
				await mark('the Memory list shows the warning on the health row, unopened');
				await listRowSays(page, '2 need attention');
				expect((await listRow(page).locator('.st-dot.warn').count()) === 1, 'the health row carries a warning dot');
				expect((await pageNotice(page).count()) === 0, 'no warning notice in the page');
				await mark('opening the row shows the check and its details');
				await listRow(page).click();
				await valueIs(page, '2 need attention');
				expect((await hint(page).textContent()) === 'Checked 12m ago', 'the hint says when it checked');
				await details(page).waitFor({ timeout: 5_000 });
				expect((await details(page).textContent()).includes('The search index is 40 facts behind.'), 'the details list each signal');
				expect((await pageNotice(page).count()) === 0, 'no warning notice in the detail pane');
				await page.waitForTimeout(800);
			}
		},
		lateAttention('desktop'),
		lateAttention('phone'),
		searchArrival,
		{
			// A loss never reaches the sidebar: the owner cannot act on it, so a
			// Needs you item would never go. The app does not ask at start either;
			// only Settings > Memory does.
			name: 'attention-stays-out-of-needs-you',
			...variants,
			async setup({ page, status, variant }) {
				await answerHealth(page, attention());
				await gotoApp(page, status);
				if (variant.viewport === 'phone') await showSidebar(page);
				await page.getByRole('complementary', { name: 'Chats' }).locator('.sect').first().waitFor({ timeout: 15_000 });
			},
			async act({ page, mark }) {
				await mark('the chats sidebar with the memory check reporting a loss');
				await page.waitForTimeout(2000);
				expect(page.memoryHealth.hits === 0, `the app does not ask outside Settings (${page.memoryHealth.hits} requests)`);
				expect((await page.locator('[data-needs-item]').count()) === 0, 'no memory item in Needs you');
				expect((await page.getByText('Memory needs attention').count()) === 0, 'no "Memory needs attention" anywhere');
				expect((await lane(page).count()) === 0, 'no Needs you lane with nothing waiting');
				await mark('Settings > Memory still shows the loss on its health row');
				await page.getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('button', { name: 'Settings', exact: true }).click();
				await page.getByRole('navigation', { name: 'Settings', exact: true }).getByRole('button', { name: 'Memory', exact: true }).filter({ visible: true }).first().click();
				await listRowSays(page, '2 need attention');
				await page.waitForTimeout(500);
			}
		},
		{
			name: 'never-checked',
			...variants,
			async setup({ page, status, variant }) {
				await answerHealth(page, attention(null));
				await openHealth(page, status, variant);
			},
			async act({ page, mark }) {
				await mark('an answer that was never checked lands');
				await valueIs(page, '2 need attention');
				expect((await hint(page).textContent()) === 'Not checked yet', `a null checked_at says so: ${await hint(page).textContent()}`);
				await mark('the details list has no check time');
				await details(page).waitFor();
				expect(!(await details(page).textContent()).includes('Last checked'), 'no check time when it never ran');
				await page.waitForTimeout(500);
			}
		}
	]
});
