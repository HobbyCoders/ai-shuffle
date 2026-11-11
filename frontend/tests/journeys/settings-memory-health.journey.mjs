// Settings > System > Memory health and its sidebar "Needs you" item against
// synthetic answers (never the owner's memory): healthy stays quiet in both
// places, a loss is one Needs you item that opens Memory health (mouse and
// keyboard) and goes when the check is healthy, a late answer moves nothing
// on the page, and a store never checked says so (#1042).
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
const WARNING = 'Capacity: Automatic cleanup has recorded 3 permanent deletions of older facts (3 at the archive limit; 0 on expiry). 1 more check needs attention.';

const HEALTH = /\/api\/v1\/memory\/health(\?|$)/;
const row = (page) => page.locator('[data-setting-anchor="system.memoryHealth"]').filter({ visible: true }).first();
const hint = (page) => row(page).locator('.st-rh');
const toggle = (page) => row(page).locator('button[aria-expanded]');
// The page carries no warning of its own any more: the sidebar item does.
const pageNotice = (page) => page.locator('.settings-viewport .st-banner').filter({ visible: true });
const lane = (page) => page.locator('[data-group="needs-you"]').filter({ visible: true });
const laneHead = (page) => page.getByRole('complementary', { name: 'Chats' }).locator('button.sect.needs').filter({ visible: true });
const memoryItem = (page) => page.locator('[data-needs-item="memory-health"] button').filter({ visible: true });
const help = (page) => page.locator('[data-setting-anchor="system.reportProblem"]').filter({ visible: true }).first();
const top = (locator) => locator.evaluate((el) => Math.round(el.getBoundingClientRect().top));

/**
 * Answer the health check with `payload` (the app asks at start and again as
 * Settings > System opens). With `held`, the answer waits until
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

/** The Needs you item is on screen (the drawer opened on a phone), saying what is wrong. */
async function itemOnScreen(page, variant) {
	if (variant.viewport === 'phone') await showSidebar(page);
	await memoryItem(page).waitFor({ timeout: 15_000 });
	const text = (await memoryItem(page).textContent()).replace(/\s+/g, ' ');
	expect(text.includes('Memory needs attention') && text.includes(WARNING), `the item says what is wrong: ${text}`);
	const head = (await laneHead(page).textContent()).replace(/\s+/g, ' ').trim();
	expect(head === 'Needs you 1', `the lane counts it: ${head}`);
	const box = await memoryItem(page).boundingBox();
	const line = await memoryItem(page).evaluate((el) => el.querySelector('.l2')?.getBoundingClientRect().height ?? 0);
	expect(box && box.height < 60 && line > 0 && line < 22, `one row, its reason on one line (${box?.height}px, line ${line}px)`);
}

/** Settings > System is open with Memory health opened and on screen. */
async function landedOnMemoryHealth(page) {
	await page.getByRole('heading', { name: 'System', exact: true }).first().waitFor({ timeout: 15_000 });
	await page.waitForFunction(() => document.querySelector('[data-setting-anchor="system.memoryHealth"] button[aria-expanded="true"]'), null, { timeout: 15_000 });
	const list = page.locator('.mh-list').filter({ visible: true });
	await list.waitFor({ timeout: 10_000 });
	expect((await list.textContent()).includes(LOSS), 'the open list has the full reason');
	// The reveal scrolls smoothly: wait for the row to come to rest on screen.
	await page
		.waitForFunction(
			() => {
				const box = document.querySelector('[data-setting-anchor="system.memoryHealth"]')?.getBoundingClientRect();
				return box && box.top >= 0 && box.top < innerHeight;
			},
			null,
			{ timeout: 5000 }
		)
		.catch(() => {});
	const box = await row(page).boundingBox();
	expect(box && box.y >= 0 && box.y < page.viewportSize().height, `the row is on screen (${box?.y})`);
}

/** Leave Settings for the chats, with the sessions sidebar showing (the drawer on a phone). */
async function backToChats(page, variant) {
	const back = page.getByRole('button', { name: 'Back to Settings', exact: true }).filter({ visible: true });
	if (await back.count()) await back.first().click();
	await page.getByRole('button', { name: 'Back to chats', exact: true }).filter({ visible: true }).first().click();
	if (variant.viewport === 'phone') await showSidebar(page);
	await page.getByRole('complementary', { name: 'Chats' }).locator('.sect').first().waitFor({ timeout: 10_000 });
}

async function openSystem(page, status, variant) {
	await gotoApp(page, status);
	if (variant.viewport === 'phone') await showSidebar(page);
	await page.getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('button', { name: 'Settings', exact: true }).click();
	await page.getByRole('navigation', { name: 'Settings', exact: true }).getByRole('button', { name: 'System', exact: true }).filter({ visible: true }).first().click();
	await page.getByRole('heading', { name: 'System', exact: true }).first().waitFor({ timeout: 15_000 });
	await row(page).waitFor({ timeout: 15_000 });
}

const valueIs = (page, text) =>
	page.waitForFunction(
		(want) => [...document.querySelectorAll('[data-testid="memory-health"]')].some((el) => el.checkVisibility() && el.textContent.trim() === want),
		text,
		{ timeout: 15_000 }
	);

const positions = async (page) => ({ row: await top(row(page)), help: await top(help(page)) });

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
 * grace pass so a later move counts. Returns where the row and Help sit.
 */
async function scrollToRow(page) {
	await row(page).evaluate((el) => el.scrollIntoView({ block: 'center' }));
	await page.waitForTimeout(800);
	return positions(page);
}

/**
 * An attention answer that lands four seconds late, after the reader has
 * scrolled the row into view. Only the row's value and hint change: the
 * warning is the sidebar's Needs you item, so nothing on the page moves.
 */
function lateAttention(viewport) {
	return {
		name: 'attention-late-after-scroll',
		viewports: [viewport],
		throttles: ['fast', 'slow'],
		async setup({ page, status, variant }) {
			await answerHealth(page, attention(), { held: true });
			await openSystem(page, status, variant);
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
			expect(after.row === before.row && after.help === before.help, `nothing moved: row ${before.row}→${after.row}, Help ${before.help}→${after.help}`);
			expect((await hint(page).textContent()) === 'Checked 12m ago', 'the hint says when it checked');
			expect((await pageNotice(page).count()) === 0, 'no notice in the page flow');
		}
	};
}

/**
 * The same late answer after arriving by Settings search from a scrolled Logs
 * page. Chromium's scroll anchoring scrolled the page by the old notice's
 * height when it landed, moving the row the search took the reader to up by
 * 67px. System turns scroll anchoring off (an error line can still land there). Logs gets a fixed answer, so the scrolled
 * start does not depend on how much the worker has logged.
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
		...DEV_INSTANCE_NOISE,
		{ kind: 'failed_requests', status: 403, url: /\/terminals(\?|$)/, reason: 'the preview cookie may not use terminals (require_terminal_owner refuses preview access by design)' },
		{ kind: 'failed_requests', status: 403, url: /\/updates\/channel(\?|$)/, reason: 'the preview cookie may not read the update channel (owner-only by design)' }
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
				await openSystem(page, status, variant);
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
				expect(after.row === before.row && after.help === before.help, `nothing moved: row ${before.row}→${after.row}, Help ${before.help}→${after.help}`);
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
				await openSystem(page, status, variant);
			},
			async act({ page, mark }) {
				await mark('an attention answer lands with the page');
				await valueIs(page, '2 need attention');
				expect((await hint(page).textContent()) === 'Checked 12m ago', 'the hint says when it checked');
				expect((await pageNotice(page).count()) === 0, 'the warning is the sidebar item, not a notice in the page');
				expect((await toggle(page).getAttribute('aria-expanded')) === 'false', 'the row stays closed');
				await page.waitForTimeout(800);
			}
		},
		lateAttention('desktop'),
		lateAttention('phone'),
		searchArrival,
		{
			// A loss is one Needs you item, alone in the lane here; clicking it
			// opens Settings > System with Memory health open and in view.
			name: 'needs-you-item-click',
			...variants,
			async setup({ page, status, variant }) {
				await answerHealth(page, attention());
				await gotoApp(page, status);
				await itemOnScreen(page, variant);
			},
			async act({ page, mark }) {
				await mark('the owner clicks the memory item in Needs you');
				await memoryItem(page).click();
				await landedOnMemoryHealth(page);
				await page.waitForTimeout(800);
			}
		},
		{
			// The item is in the lane's tab order, right after the lane header,
			// and Enter opens it.
			name: 'needs-you-item-keyboard',
			...variants,
			async setup({ page, status, variant }) {
				await answerHealth(page, attention());
				await gotoApp(page, status);
				await itemOnScreen(page, variant);
			},
			async act({ page, mark }) {
				await mark('the owner tabs from the lane header to the item and presses Enter');
				await laneHead(page).focus();
				await page.keyboard.press('Tab');
				const focused = await page.evaluate(() => document.activeElement?.closest('[data-needs-item="memory-health"]') != null);
				expect(focused, 'Tab from the lane header lands on the item');
				await page.keyboard.press('Enter');
				await landedOnMemoryHealth(page);
				await page.waitForTimeout(800);
			}
		},
		{
			// It follows the check: the next answer being healthy (Settings asks
			// again as System opens) takes it away, and the lane with it.
			name: 'needs-you-item-clears',
			...variants,
			async setup({ page, status, variant }) {
				await answerHealth(page, attention());
				await gotoApp(page, status);
				await itemOnScreen(page, variant);
			},
			async act({ page, variant, mark }) {
				await mark('the check reports healthy as System opens');
				page.memoryHealth.answer(healthy());
				await page.getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('button', { name: 'Settings', exact: true }).click();
				await page.getByRole('navigation', { name: 'Settings', exact: true }).getByRole('button', { name: 'System', exact: true }).filter({ visible: true }).first().click();
				await valueIs(page, 'Healthy, 1 note');
				await mark('back in the chats, the lane is gone');
				await backToChats(page, variant);
				await page.waitForFunction(() => !document.querySelector('[data-needs-item]'), null, { timeout: 10_000 });
				expect((await lane(page).count()) === 0, 'the lane it was alone in is gone');
				await page.waitForTimeout(800);
			}
		},
		{
			name: 'never-checked',
			...variants,
			async setup({ page, status, variant }) {
				await answerHealth(page, attention(null));
				await openSystem(page, status, variant);
			},
			async act({ page, mark }) {
				await mark('an answer that was never checked lands');
				await valueIs(page, '2 need attention');
				expect((await hint(page).textContent()) === 'Not checked yet', `a null checked_at says so: ${await hint(page).textContent()}`);
				await mark('the open list has no check time');
				await toggle(page).click();
				const list = page.locator('.mh-list').filter({ visible: true });
				await list.waitFor();
				expect(!(await list.textContent()).includes('Last checked'), 'no check time when it never ran');
				await page.waitForTimeout(500);
			}
		}
	]
});
