// Journey: a message queued behind an open, idle turn must sit below the
// running-task row, never on top of it. In a long turn the turn's own prompt
// is no longer mounted, and the room under the transcript then left out the
// queued bubbles: they grew up over the "N running tasks" row (phone report).
import { runJourney, DEFAULT_VIEWPORTS } from './runner.mjs';
import { PROJECT, expect, openNewChat, pickAccess, scriptTag, typeAndSend, uniq, waitChat, waitTurnSettled } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

/** The running-task row, in flow at the end of the transcript. */
const TASK_ROW = '.chat-column > .inline';
/** How long the bubble is watched, frame by frame, once it is confirmed. */
const WATCH_MS = 1500;

/** Stop asks first when background work is running, since it ends that too. */
async function stopEverything(page) {
	await page.getByRole('button', { name: 'Stop generating' }).click();
	await page.getByRole('button', { name: 'Stop everything' }).click();
}

/** Until the transcript's rows and the task row hold still for 1.5s. */
function waitIdle(page) {
	return page.evaluate(({ taskRow }) => new Promise((done, fail) => {
		const started = performance.now();
		let last = ''; let since = started;
		const read = () => {
			const column = document.querySelector('[data-ui-mount="MessageArea"] .chat-column');
			const row = column?.querySelector(taskRow.replace('.chat-column', ':scope'));
			const now = performance.now();
			const key = `${column?.querySelectorAll(':scope > .msg-virtualized').length}|${Math.round(row?.getBoundingClientRect().height ?? -1)}|${Math.round(column?.scrollHeight ?? 0)}`;
			if (key !== last) { last = key; since = now; }
			if (now - since >= 1500) return done();
			if (now - started > 30_000) return fail(new Error(`the turn never sat still: ${key}`));
			setTimeout(read, 100);
		};
		read();
	}), { taskRow: TASK_ROW });
}

/**
 * Every painted frame for `ms`: the queued bubbles' top against the bottom of
 * the running-task row and of the last mounted transcript row, plus the room
 * reserved under the transcript and the queue's own height.
 */
function watchTail(page, ms) {
	return page.evaluate(({ ms, taskRow }) => new Promise((done) => {
		const frames = [];
		const started = performance.now();
		const read = () => {
			const queue = document.querySelector('.pending-messages');
			// The queue's own column: another surface can hold a .chat-column too.
			const column = queue?.closest('.chat-column') ?? null;
			const row = column?.querySelector(taskRow.replace('.chat-column', ':scope')) ?? null;
			const rows = column?.querySelectorAll(':scope > .msg-virtualized') ?? [];
			const last = rows[rows.length - 1];
			const controls = column?.querySelector('.tail-controls') ?? null;
			const round = (n) => (n == null ? null : Math.round(n * 10) / 10);
			frames.push({
				at: Math.round(performance.now() - started),
				queueTop: round(queue?.getBoundingClientRect().top),
				rowBottom: round(row?.getBoundingClientRect().bottom),
				lastBottom: round(last?.getBoundingClientRect().bottom),
				reserve: round(column ? parseFloat(getComputedStyle(column).paddingBottom) : null),
				controls: round(controls?.getBoundingClientRect().height)
			});
			if (performance.now() - started < ms) requestAnimationFrame(read);
			else done(frames);
		};
		requestAnimationFrame(read);
	}), { ms, taskRow: TASK_ROW });
}

await runJourney({
	name: 'queue-over-running-tasks',
	cases: [{
		name: 'queued-bubble-below-running-tasks-when-the-prompt-is-unmounted',
		viewports: [...DEFAULT_VIEWPORTS, 'fold-inner'],
		async setup(ctx) {
			await openNewChat(ctx, { project: PROJECT });
			// Shell tools need a project; Bypass so the job starts without approvals.
			await pickAccess(ctx.page, 'Bypass');
			await ctx.page.waitForTimeout(800);
			await typeAndSend(ctx.page, `history tok-${uniq()} ${scriptTag(ctx.variant, 'long', { tps: 400 })}`);
			await waitTurnSettled(ctx.page, { text: 'Here is a long answer', messagesAtLeast: 2, timeout: 90_000 });
			// Unrecorded: the running-task row moves as tool rows land above it on
			// a phone, a move of its own that predates and is apart from this case.
			const { page, variant } = ctx;
			const job = `job tok-${uniq()}`;
			// A reply past the mounted-content budget, then a two-minute Monitor and
			// a one-minute Bash: the turn stays open and idle, the task row shows,
			// and the turn's prompt is no longer mounted.
			await typeAndSend(page, `${job} ${scriptTag(variant, 'tool-background-heavy', { tps: 4000, latency_ms: 0 })}`);
			await page.locator('[data-ui-mount="MessageArea"]').getByTestId('tasks-toggle').waitFor({ timeout: 60_000 });
			await page.getByText('Waiting on it. Meanwhile', { exact: false }).last().waitFor({ timeout: 60_000 });
			await waitChat(page, { streaming: 'true' }, { timeout: 30_000 });
			// Queue only once the turn sits idle: a late row or task-row change
			// would move the queue, which is not this bug.
			await waitIdle(page);
			const promptMounted = await page.evaluate((job) => [...document.querySelectorAll('[data-user-turn-id]')]
				.some((el) => el.textContent.includes(job)), job);
			expect(!promptMounted, 'the open turn\'s prompt is outside the mounted rows, or this case proves nothing');
			expect(await page.locator(TASK_ROW).count() === 1, 'the running-task row is in the transcript');
		},
		async act({ page, mark, log }) {
			await mark('queue a two-line message');
			const queued = `tok-${uniq()}`;
			await typeAndSend(page, `queued ${queued}\nsecond line of the queued message`);
			await page.locator('.pending-messages [data-queue-confirmed="true"]').filter({ hasText: queued }).waitFor({ timeout: 30_000 });
			const frames = await watchTail(page, WATCH_MS);
			const overlaps = frames.filter((f) => f.queueTop !== null && f.rowBottom !== null && f.queueTop < f.rowBottom - 0.5);
			const settled = frames.at(-1);
			log(`[at, bubble top, task row bottom, last row bottom, reserve, controls] first ${JSON.stringify(Object.values(frames[0]))} last ${JSON.stringify(Object.values(settled))}; overlapping frames ${overlaps.length}/${frames.length}`);
			expect(settled.controls > 40, `the queue has height (${settled.controls}px)`);
			expect(overlaps.length === 0, `the queued bubble stays below the running-task row in every frame: ${JSON.stringify(overlaps.slice(0, 3))}`);
			expect(settled.lastBottom === null || settled.queueTop >= settled.lastBottom - 0.5, `the queued bubble stays below the last transcript row: ${JSON.stringify(settled)}`);
			expect(settled.reserve >= settled.controls + 24, `the room under the transcript holds the queue (${settled.reserve}px for ${settled.controls}px)`);
		},
		// Unrecorded: a resize and Stop each move the layout on their own.
		async verify({ page, variant, log }) {
			if (variant.viewport === 'phone') {
				// Unfolding: wider prose ends higher. The room kept under the transcript
				// must follow it, not hold the narrow layout's end as blank viewport.
				const { height } = page.viewportSize();
				await page.setViewportSize({ width: 700, height });
				await page.waitForTimeout(600);
				const after = (await watchTail(page, 300)).at(-1);
				log(`after widening to 700px: ${JSON.stringify(after)}`);
				expect(after.rowBottom !== null && after.rowBottom > 0, `the running-task row is still on screen after widening, not scrolled off above blank room: ${JSON.stringify(after)}`);
				expect(after.queueTop >= after.rowBottom - 0.5, `the queued bubble stays below the running-task row after widening: ${JSON.stringify(after)}`);
			}
			await stopEverything(page);
			await page.waitForFunction((sel) => document.querySelector(sel) === null, TASK_ROW, { timeout: 15_000 });
			await waitChat(page, { streaming: 'false' }, { timeout: 15_000 });
		}
	}],
	allow: [...DEV_INSTANCE_NOISE]
});
