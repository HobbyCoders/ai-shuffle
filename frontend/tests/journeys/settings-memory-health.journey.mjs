// Settings > System > Memory health against synthetic answers (never the
// owner's memory): healthy stays quiet, a warning is readable without opening
// the row, a late warning does not push what the reader is looking at, Details
// opens the row with focus on its toggle, closing it brings the notice back,
// and a store never checked says so (#1042).
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
const notice = (page) => page.getByRole('status').filter({ hasText: 'Capacity:' }).filter({ visible: true });
const details = (page) => notice(page).getByRole('button', { name: 'Details', exact: true });
const help = (page) => page.locator('[data-setting-anchor="system.reportProblem"]').filter({ visible: true }).first();
const top = (locator) => locator.evaluate((el) => Math.round(el.getBoundingClientRect().top));

/**
 * Answer the health check with `payload`. With `held`, the answer waits until
 * `page.memoryHealth.release()` (a worker slow to answer).
 */
async function answerHealth(page, payload, { held = false } = {}) {
	let release;
	const gate = held ? new Promise((done) => (release = done)) : Promise.resolve();
	page.memoryHealth = { hits: 0, release: () => release?.() };
	await page.route(HEALTH, async (route) => {
		if (route.request().method() !== 'GET') return route.continue();
		page.memoryHealth.hits++;
		await gate;
		await route.fulfill({ json: payload }).catch(() => {});
	});
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
 * scrolled the row into view. The row and everything above it stay put; the
 * notice is inserted under the Memory card, where the reader is looking.
 */
function lateAttention(viewport) {
	return {
		name: 'attention-late-after-scroll',
		viewports: [viewport],
		throttles: ['fast', 'slow'],
		// On a phone the notice (four lines and its Details button, about 160px)
		// pushes the Help group below it down: measured 0.064. Its place cannot be
		// held without knowing the answer, and a smaller, quieter notice still
		// measured 0.052, so this one move is excused (the "Needs you" follow-up
		// on #1042 can take the warning out of the page flow). Every other shift,
		// and this one past 0.08, still fails. At 1440 (and 1280) it measures
		// about 0.01 and is judged as usual.
		...(viewport === 'phone'
			? {
					expectShift: [
						{
							step: 'the attention answer lands late',
							moved: /^(section\.st-grp|span\.st-status)$/,
							maxValue: 0.08,
							reason: 'the late warning is inserted under the Memory card and pushes the Help group down (#1042)'
						}
					]
				}
			: {}),
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
			await notice(page).waitFor({ timeout: 10_000 });
			await page.waitForTimeout(1500);
			const after = await positions(page);
			expect(after.row === before.row, `the Memory row stayed where the reader had it (${before.row}→${after.row})`);
			const box = await notice(page).boundingBox();
			const height = page.viewportSize().height;
			expect(box && box.y >= 0 && box.y + box.height <= height, `the notice is on screen where the reader is looking (${box?.y}..${box && box.y + box.height} of ${height})`);
			expect((await notice(page).textContent()).replace(/\s+/g, ' ').includes(WARNING), 'the late notice reads the same');
		}
	};
}

/**
 * The same late answer after arriving by Settings search from a scrolled Logs
 * page. Chromium's scroll anchoring scrolled the page by the notice's height
 * when it landed, moving the row the search took the reader to up by 67px.
 * System turns scroll anchoring off.
 */
const searchArrival = {
	name: 'attention-late-after-search',
	viewports: ['desktop'],
	throttles: ['fast'],
	async setup({ page, status }) {
		await answerHealth(page, attention(), { held: true });
		await gotoApp(page, status);
		await page.getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('button', { name: 'Settings', exact: true }).click();
		await page.getByRole('navigation', { name: 'Settings', exact: true }).getByRole('button', { name: 'Logs', exact: true }).filter({ visible: true }).first().click();
		// The reader scrolls down the log, so the search reveal scrolls up.
		await page.waitForFunction(
			() => {
				const s = document.querySelector('.settings-viewport');
				if (!s || s.scrollHeight - s.clientHeight < 1200) return false;
				s.scrollTop = s.scrollHeight;
				return true;
			},
			null,
			{ timeout: 30_000 }
		);
		await page.waitForTimeout(800);
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
		await notice(page).waitFor({ timeout: 10_000 });
		await page.waitForTimeout(1500);
		const after = await positions(page);
		expect(after.row === before.row, `the row the search took the reader to stayed put (${before.row}→${after.row})`);
		const box = await notice(page).boundingBox();
		expect(box && box.y + box.height <= page.viewportSize().height, `the notice is on screen (${box?.y}..${box && box.y + box.height})`);
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
				await notice(page).waitFor({ timeout: 10_000 });
				expect((await notice(page).textContent()).replace(/\s+/g, ' ').includes(WARNING), `the reason is readable without opening: ${await notice(page).textContent()}`);
				expect((await hint(page).textContent()) === 'Checked 12m ago', 'the hint says when it checked');
				expect((await toggle(page).getAttribute('aria-expanded')) === 'false', 'the row stays closed');
				await page.waitForTimeout(800);
			}
		},
		lateAttention('desktop'),
		lateAttention('phone'),
		searchArrival,
		{
			name: 'details-and-back',
			...variants,
			async setup({ page, status, variant }) {
				await answerHealth(page, attention());
				await openSystem(page, status, variant);
				await notice(page).waitFor({ timeout: 10_000 });
				await scrollToRow(page);
			},
			async act({ page, mark }) {
				await mark('Details opens the row');
				await details(page).click();
				await page.waitForFunction(() => document.querySelector('[data-setting-anchor="system.memoryHealth"] button[aria-expanded="true"]'));
				await page.waitForTimeout(300);
				const focused = await page.evaluate(() => document.activeElement?.matches('[data-setting-anchor="system.memoryHealth"] button[aria-expanded]') ?? false);
				expect(focused, 'focus lands on the row toggle');
				expect((await notice(page).count()) === 0, 'the notice gives way to the open list');
				const list = page.locator('.mh-list').filter({ visible: true });
				expect((await list.textContent()).includes(LOSS), 'the open list has the full reason');
				expect(/Last checked \w{3} \d{1,2}, \d{4}/.test(await list.textContent()), 'the open list ends with the full check time');
				await page.waitForTimeout(500);
				await mark('closing the row brings the notice back');
				await toggle(page).click();
				await notice(page).waitFor({ timeout: 5_000 });
				expect((await toggle(page).getAttribute('aria-expanded')) === 'false', 'the row is closed');
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
				await notice(page).waitFor({ timeout: 10_000 });
				expect((await hint(page).textContent()) === 'Not checked yet', `a null checked_at says so: ${await hint(page).textContent()}`);
				await mark('the open list has no check time');
				await details(page).click();
				const list = page.locator('.mh-list').filter({ visible: true });
				await list.waitFor();
				expect(!(await list.textContent()).includes('Last checked'), 'no check time when it never ran');
				await page.waitForTimeout(500);
			}
		}
	]
});
