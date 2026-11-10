// Journey: Stop with messages queued must not move what the person is reading,
// when the rows land or after the turn settles. Positions are read off the DOM
// on every painted frame, so a move under the recorder's threshold still shows.
// Rows past the fold are one deliberate tap on Jump to latest away.
import { runJourney } from './runner.mjs';
import {
	PROJECT,
	composer,
	expect,
	pickAccess,
	pickProject,
	scriptTag,
	scrollTranscriptTo,
	sendButton,
	stopButton,
	transcriptScroll,
	typeAndSend,
	uniq,
	waitChat,
	waitTurnSettled
} from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
import { STOP_SELECTOR } from './lib/timeline.mjs';
import { openStopChat, watchStopFrames, PAST_SETTLEMENT_MS, queueDuringASlowTurn, expectEachOnceInEveryFrame } from './lib/stop-queue.mjs';

/** One more line of the stopped reply can still arrive and nudge what is below it. */
const REPLY_LINE_PX = 30;

/** Measured on a slow phone: at most 0.017 when the follow lands in the same task, 0.073 to 0.095 when it does not. */
const BUBBLE_SHIFT_TOTAL = 0.05;

/**
 * Something in the transcript changes height long after the turn is over, as a
 * late image does. A reader who is not at the very bottom must not be taken there.
 */
function growTranscriptOnePixel(page) {
	return page.evaluate(() => {
		const spacer = document.querySelector('.transcript-tail-spacer');
		spacer.style.marginTop = `${parseFloat(spacer.style.marginTop || '0') + 1}px`;
	});
}

const jumpToLatestButton = (page) => page.getByRole('button', { name: 'Jump to latest message' });

/** Stop asks first when background work is running, since it ends that too. */
async function stopEverything(page) {
	await page.getByRole('button', { name: 'Stop generating' }).click();
	await page.getByRole('button', { name: 'Stop everything' }).click();
}

/** The running-task row above the queue, read per painted frame with the rest (#1147). */
const TASK_ROW = '.chat-column > .inline';
const taskRowShown = (page) => page.evaluate((sel) => document.querySelector(sel) !== null, TASK_ROW);

/** A turn that starts a real background job and stays open, with messages queued behind it. */
async function queueBehindABackgroundJob(page, mark, variant, queued) {
	await mark('start a background job');
	// A turn taller than the screen, so the room under it is the minimum and the
	// row's own height is all that holds the scroll range up. The turn then waits
	// on a minute-long command: still content, still open, until Stop.
	await typeAndSend(page, `job tok-${uniq()} ${scriptTag(variant, 'tool-background-long', { tps: 400, latency_ms: 0 })}`);
	await page.locator('[data-ui-mount="MessageArea"]').getByTestId('tasks-toggle').waitFor({ timeout: 60_000 });
	await page.getByText('Waiting on it.', { exact: false }).last().waitFor({ timeout: 60_000 });
	await page.waitForTimeout(1000);
	await waitChat(page, { streaming: 'true' }, { timeout: 30_000 });
	await mark('queue');
	const texts = Array.from({ length: queued }, () => `tok-${uniq()}`);
	for (const text of texts) {
		await typeAndSend(page, `queued ${text}`);
		await page.locator('.pending-messages [role="article"]').filter({ hasText: text }).waitFor({ timeout: 5000 });
	}
	await page.waitForFunction(
		(n) => document.querySelectorAll('.pending-messages [data-queue-confirmed="true"]').length === n,
		texts.length,
		{ timeout: 30_000 }
	);
	return texts;
}

/**
 * Stop everything with a background job running ends that job too, and its
 * running-task row leaves the transcript above the queued bubbles (#1147). In
 * the frame it leaves, nothing on screen may move: not the line being read, not
 * the bubbles, not the scroll position.
 */
const stopWithARunningTask = {
	async setup(ctx) {
		await openStopChat(ctx);
		// Shell tools need a project; Bypass so the job starts without approvals.
		await pickProject(ctx.page, ctx.page.getByRole('region', { name: 'New chat' }), PROJECT);
		await pickAccess(ctx.page, 'Bypass');
		await ctx.page.waitForTimeout(800);
		await typeAndSend(ctx.page, `history tok-${uniq()} ${scriptTag(ctx.variant, 'long', { tps: 400 })}`);
		await waitTurnSettled(ctx.page, { text: 'Here is a long answer', messagesAtLeast: 2, timeout: 90_000 });
	},
	async act({ page, mark, variant, log }) {
		const texts = await queueBehindABackgroundJob(page, mark, variant, 2);
		const collect = await watchStopFrames(page, texts);
		await mark('stop');
		expect(await taskRowShown(page), 'the running-task row is in the transcript before Stop');
		await stopEverything(page);
		await page.waitForFunction((sel) => document.querySelector(sel) === null, TASK_ROW, { timeout: 15_000 });
		await waitChat(page, { streaming: 'false' }, { timeout: 15_000 });
		for (const text of texts) {
			await page.locator('[data-not-sent="true"]').filter({ hasText: text }).waitFor({ timeout: 5000 });
		}
		await page.waitForTimeout(500);
		const frames = await collect();
		const geometry = frames.filter((f, i) => i === 0 || ['scrollTop', 'scrollHeight', 'taskRow', 'lineTop'].some((k) => f[k] !== frames[i - 1][k]) || f.messages.some((m, j) => m.top !== frames[i - 1].messages[j].top));
		log(`[at, scrollTop, scrollHeight, task row, first line on screen, queued tops] ${JSON.stringify(geometry.map((f) => [f.at, f.scrollTop, f.scrollHeight, f.taskRow, f.lineTop, f.messages.map((m) => `${m.bubbles ? 'b' : 'r'}${m.top}`).join(' ')]))}`);
		const gone = frames.findIndex((f, i) => i > 0 && f.taskRow === null && frames[i - 1].taskRow !== null);
		expect(gone > 0, `watched the running-task row leave (${frames.length} frames)`);
		const [was, now] = [frames[gone - 1], frames[gone]];
		expect(was.taskRow > 40, `the row had height before it left (${was.taskRow}px)`);
		expect(was.lineTop !== null && now.lineTop !== null, 'the first line on screen was found on both sides of the removal');
		const lineMoved = Math.abs(now.lineTop - was.lineTop);
		const scrollMoved = Math.abs(now.scrollTop - was.scrollTop);
		log(`task row left at ${now.at}ms: line moved ${lineMoved}px, scrollTop ${was.scrollTop} -> ${now.scrollTop}`);
		expect(lineMoved <= 1, `the line being read stayed put as the row left (moved ${lineMoved}px)`);
		expect(scrollMoved <= 1, `the scroll position held as the row left (${was.scrollTop} -> ${now.scrollTop})`);
		now.messages.forEach((message, m) => {
			const before = was.messages[m];
			// A slow browser can paint the row leaving and the bubble becoming its
			// "Not sent" row as one frame. That move is the bubble's own, not the row's.
			if (message.bubbles !== 1 || before.bubbles !== 1) return log(`queued message ${m} became its "Not sent" row in the frame the row left`);
			const moved = Math.abs(message.top - before.top);
			expect(moved <= 1, `queued message ${m} stayed put as the row left (moved ${moved}px)`);
		});
	}
};

/** Stop with queued messages must not move what the person is reading, then or later. */
function stopReadingPosition({ scrolled, queued = 3, overflows = false, rounds = 1 }) {
	return {
		async setup(ctx) {
			await openStopChat(ctx);
			await ctx.page.waitForTimeout(800);
			await typeAndSend(ctx.page, `history tok-${uniq()} ${scriptTag(ctx.variant, 'long', { tps: 400 })}`);
			await waitTurnSettled(ctx.page, { text: 'Here is a long answer', messagesAtLeast: 2, timeout: 90_000 });
		},
		async act({ page, mark, variant, log }) {
			for (let round = 0; round < rounds; round++) {
				const texts = await queueDuringASlowTurn(page, mark, variant, queued);
				if (scrolled) {
					await mark('scroll up');
					await scrollTranscriptTo(page, 0.2);
					await page.waitForTimeout(400);
				}
				// A new bubble gives its blank reserve back one frame later: the scroll range
				// is briefly larger with the reader unmoved. Read the start after that frame.
				// A reader really left above the bottom still fails the check below.
				if (!scrolled) {
					await page.waitForFunction(() => {
						const log = document.querySelector('[role="log"][aria-label="Conversation messages"]');
						return log.scrollHeight - log.clientHeight - log.scrollTop <= 48;
					}, null, { timeout: 2000 }).catch(() => {});
				}
				const before = await transcriptScroll(page);
				expect(before.atBottom === !scrolled, `the reader starts ${scrolled ? 'scrolled up' : 'at the bottom'}: ${JSON.stringify(before)}`);

				const collect = await watchStopFrames(page, texts);
				await mark('stop');
				await stopButton(page).click();
				await waitChat(page, { streaming: 'false' }, { timeout: 15_000 });
				for (const text of texts) {
					await page.locator('[data-not-sent="true"]').filter({ hasText: text }).waitFor({ timeout: 5000 });
				}
				await page.getByText('[Stopped]', { exact: false }).first().waitFor({ timeout: 5000 });
				await page.waitForTimeout(PAST_SETTLEMENT_MS);
				await mark('late height change');
				await growTranscriptOnePixel(page);
				await page.waitForTimeout(500);
				const frames = await collect();
				const after = await transcriptScroll(page);

				expect(frames.length > 10, `watched the frames across Stop (${frames.length})`);
				const lastFrameAt = frames[frames.length - 1].at;
				expect(lastFrameAt >= PAST_SETTLEMENT_MS, `watched past settlement (${lastFrameAt}ms)`);
				const tops = frames.map((f) => f.anchorTop);
				expect(tops.every((top) => top !== null), 'the row being read stayed mounted');
				const anchorMoved = Math.max(...tops) - Math.min(...tops);
				log(`round ${round}: row being read moved ${anchorMoved}px; scrollTop ${before.top} -> ${after.top}; ${frames.length} frames over ${lastFrameAt}ms`);
				const steps = frames.filter((f, i) => i === 0 || f.anchorTop !== frames[i - 1].anchorTop);
				log(`row top by time: ${steps.map((f) => `${f.at}ms ${f.anchorTop} (scrollTop ${f.scrollTop})`).join(', ')}`);
				expect(anchorMoved <= 1, `the row being read stayed put (moved ${anchorMoved}px)`);
				const composerTops = frames.map(f => f.composerTop);
				expect(Math.max(...composerTops) - Math.min(...composerTops) <= 1, 'Stop leaves the composer docked while its action slot empties');
				expect(Math.abs(after.top - before.top) <= 1, `the scroll position held: ${before.top} -> ${after.top}`);
				if (!overflows) {
					expect(after.atBottom === !scrolled, `the reader is still ${scrolled ? 'scrolled up' : 'at the bottom'}: ${JSON.stringify(after)}`);
				} else if (variant.viewport === 'phone') {
					expect(!after.atBottom, `these rows run past a phone's fold, or this case proves nothing: ${JSON.stringify(after)}`);
				}

				expectEachOnceInEveryFrame(frames);
				frames.forEach((frame, i) => {
					frame.messages.forEach((message, m) => {
						const previous = frames[i - 1]?.messages[m];
						if (!previous || !previous.bubbles !== !message.bubbles) return;
						// Changing from bubble to row is the one move Stop is meant to make.
						const moved = Math.abs(message.top - previous.top);
						expect(moved <= REPLY_LINE_PX, `queued message ${m} did not jump in frame ${i}: moved ${moved}px as a ${message.bubbles ? 'bubble' : 'row'}; around it ${JSON.stringify(frames.slice(Math.max(0, i - 2), i + 2).map((f) => [f.at, f.scrollTop, f.anchorTop, f.replyHeight, f.messages[m].top]))}`);
					});
				});
				const kinds = new Set(frames.map((f) => f.messages.map((m) => (m.bubbles ? 'bubble' : 'row')).join()));
				expect(kinds.size === 2, `all of them changed in the same frame, not one by one: ${[...kinds].join(' | ')}`);

				// Rows past the fold are one deliberate tap away, and that tap resumes following.
				if (!scrolled && !after.atBottom) {
					await mark('jump to the rows');
					await jumpToLatestButton(page).waitFor({ timeout: 5000 });
					await jumpToLatestButton(page).click();
					await page.waitForFunction(() => {
						const log = document.querySelector('[role="log"][aria-label="Conversation messages"]');
						return log.scrollHeight - log.clientHeight - log.scrollTop <= 1;
					}, null, { timeout: 5000 });
					const last = page.locator('[data-not-sent="true"]').filter({ hasText: texts[texts.length - 1] });
					expect(await last.isVisible(), 'Jump to latest reached the last row');
					await growTranscriptOnePixel(page);
					await page.waitForFunction(() => {
						const log = document.querySelector('[role="log"][aria-label="Conversation messages"]');
						return log.scrollHeight - log.clientHeight - log.scrollTop <= 1;
					}, null, { timeout: 5000 });
				}
			}
		}
	};
}

await runJourney({
	name: 'stop-holds-the-reader',
	recorder: { selectors: [STOP_SELECTOR, 'pending=.pending-messages [role="article"]'] },
	cases: [
		// Two rows fit under a pinned prompt, so the reader is still at the bottom.
		{ name: 'stop-holds-the-reader-at-the-bottom', ...stopReadingPosition({ scrolled: false, queued: 2 }) },
		// Three rows can run past a phone's fold. Stop preserves the reading point,
		// independently of ordinary completion following the live edge (#1112).
		{ name: 'stop-holds-the-reader-when-the-rows-overflow', ...stopReadingPosition({ scrolled: false, overflows: true }) },
		// More of them, Stop after Stop in one chat with no filler in between.
		{ name: 'stop-holds-the-reader-with-four-queued', ...stopReadingPosition({ scrolled: false, queued: 4, overflows: true, rounds: 2 }) },
		{ name: 'stop-holds-the-reader-scrolled-up', ...stopReadingPosition({ scrolled: true }) },
		{
			// The frame the running-task row leaves is checked pixel by pixel in the case.
			// Later, when the stopped turn settles, its finished steps fold and the rows
			// under them move; on a phone that fold is large enough to count as a shift.
			name: 'stop-with-a-running-task-holds-the-bubbles',
			...stopWithARunningTask,
			allow: [
				{
					kind: 'layout_shift',
					message: /moved: div\.msg-virtualized in MessageArea/,
					reason: 'known bug #1147: a stopped turn folds its finished steps about 0.8 s after Stop and the Not sent rows move with it'
				}
			]
		},
		{
			// A reply arriving in large pieces, followed at the bottom. The follow has to
			// land in the frame the piece does, or for one frame everything below the
			// reply shows a piece too low: the bubbles drop and come straight back.
			name: 'stop-during-a-fast-reply-keeps-the-bubbles-still',
			async setup(ctx) {
				await openStopChat(ctx);
				await ctx.page.waitForTimeout(800);
			},
			async act({ page, mark, variant, log }) {
				await mark('send');
				// Keep the same fast chunks and reply, but only Stop can end the turn:
				// a slow browser may take longer to queue than the whole reply takes to stream.
				await typeAndSend(page, `stream tok-${uniq()} ${scriptTag(variant, 'long', { tps: 150, hold_open: true })}`);
				await page.getByText('Here is a long answer', { exact: false }).last().waitFor({ timeout: 60_000 });
				await mark('queue');
				const texts = [`tok-${uniq()}`, `tok-${uniq()}`];
				const collect = await watchStopFrames(page, texts);
				await page.evaluate(() => {
					const scores = (window.__bubbleShifts = []);
					new PerformanceObserver((list) => {
						for (const entry of list.getEntries()) {
							const moved = entry.sources.some((s) => s.node?.closest?.('.pending-messages'));
							if (moved) scores.push(Math.round(entry.value * 1000) / 1000);
						}
					}).observe({ type: 'layout-shift' });
				});
				for (const text of texts) {
					await typeAndSend(page, `queued ${text}`);
					await page.locator('.pending-messages [role="article"]').filter({ hasText: text }).waitFor({ timeout: 5000 });
				}
				await mark('stop');
				await stopButton(page).click({ timeout: 2000 });
				await waitChat(page, { streaming: 'false' }, { timeout: 60_000 });
				for (const text of texts) {
					await page.locator('[data-not-sent="true"]').filter({ hasText: text }).waitFor({ timeout: 15_000 });
				}
				const frames = await collect();
				// A follow that lands after the task that grew the reply can leave the pixels alone and
				// still score: the browser reads what is below as pushed off screen and back. Close to
				// input the recorder forgives that, so add up the bubbles' scores here whatever the timing.
				const scores = await page.evaluate(() => window.__bubbleShifts);
				const scored = scores.reduce((total, score) => total + score, 0);
				log(`bubble shift scores: ${scored.toFixed(3)} from ${JSON.stringify(scores)}`);
				expect(scored <= BUBBLE_SHIFT_TOTAL, `the reply's growth did not score against the bubbles: ${JSON.stringify(scores)}`);
				// Growth may push the bubbles down and leave them there. Down and straight back is the fault.
				// Typing and sending resize the composer, which moves them too: that is the person's own
				// input, which the recorder scores, so only frames with a still composer count here.
				const held = frames.filter((f) => f.messages[0].bubbles === 1);
				const still = (a, b) => Math.abs(a.composerTop - b.composerTop) < 1;
				let bounce = 0;
				let judged = 0;
				for (let i = 2; i < held.length; i++) {
					if (!still(held[i - 2], held[i - 1]) || !still(held[i - 1], held[i])) continue;
					judged++;
					const top = held[i - 1].messages[0].top;
					bounce = Math.max(bounce, Math.min(top - held[i - 2].messages[0].top, top - held[i].messages[0].top));
				}
				log(`biggest bubble bounce: ${bounce}px over ${judged} of ${held.length} frames`);
				expect(judged > 5, `watched the bubbles while the reply ran (${judged} frames)`);
				expect(bounce <= REPLY_LINE_PX, `the bubbles did not drop and come back in one frame: ${bounce}px; [at, scrollTop, top, composer] ${JSON.stringify(frames.map((f) => [f.at, f.scrollTop, f.messages[0].top, f.composerTop]))}`);
			}
		},
		{
			// Only what was queued when Stop was pressed can be not sent. A message
			// sent right after Stop runs, and must never read "Not sent" on the way.
			name: 'stop-then-send-is-never-labelled-not-sent',
			async setup(ctx) {
				await openStopChat(ctx);
				await ctx.page.waitForTimeout(800);
			},
			async act({ page, mark, variant, log }) {
				const texts = await queueDuringASlowTurn(page, mark, variant, 2);
				const fresh = `tok-${uniq()}`;
				const box = composer(page);
				await box.click();
				await box.fill(`fresh ${fresh} ${scriptTag(variant, 'default')}`);

				const collect = await watchStopFrames(page, [...texts, fresh]);
				await mark('stop');
				await stopButton(page).click();
				await sendButton(page).click();
				await page.getByText(`You said: “fresh ${fresh}`, { exact: false }).first().waitFor({ timeout: 60_000 });
				await page.waitForTimeout(500);
				const frames = await collect();
				const seen = frames.map((f) => f.messages[2]);
				const firstCopy = seen.findIndex((m) => m.bubbles + m.notSent > 0);
				log(`the new message: first shown at ${frames[firstCopy]?.at}ms of ${frames.length} frames; as a bubble in ${seen.filter((m) => m.bubbles).length}`);
				seen.forEach((message, i) => {
					expect(message.labelledNotSent === 0, `the new message reads "Not sent" in frame ${i} (${frames[i].at}ms)`);
					expect(message.notSent === 0, `the new message is a not-sent row in frame ${i} (${frames[i].at}ms)`);
					expect(message.bubbles <= 1, `the new message shows once in frame ${i}: ${JSON.stringify(message)}`);
				});
				for (const text of texts) {
					expect((await page.locator('[data-not-sent="true"]').filter({ hasText: text }).count()) === 1, 'what was queued at Stop is a not-sent row');
				}
			}
		},
		{
			// The rows run past the fold, which would release a reader who only pressed
			// Stop. This one also sent a message, so they asked to see it run.
			name: 'stop-then-send-in-a-long-chat-shows-the-new-reply',
			async setup(ctx) {
				await openStopChat(ctx);
				await ctx.page.waitForTimeout(800);
			},
			async act({ page, mark, variant }) {
				await mark('history');
				await typeAndSend(page, `history tok-${uniq()} ${scriptTag(variant, 'long', { tps: 400 })}`);
				await waitTurnSettled(page, { text: 'Here is a long answer', messagesAtLeast: 2, timeout: 90_000 });
				const texts = await queueDuringASlowTurn(page, mark, variant, 2);
				const fresh = `tok-${uniq()}`;
				const box = composer(page);
				await box.click();
				await box.fill(`fresh ${fresh} ${scriptTag(variant, 'default')}`);
				await mark('stop');
				await stopButton(page).click();
				await sendButton(page).click();
				const reply = page.getByText(`You said: “fresh ${fresh}`, { exact: false }).first();
				await reply.waitFor({ timeout: 60_000 });
				await waitTurnSettled(page, { text: `You said: “fresh ${fresh}`, timeout: 60_000 });
				await page.waitForTimeout(1500);
				const shown = await reply.evaluate((el) => {
					const log = document.querySelector('[role="log"][aria-label="Conversation messages"]');
					const box = el.getBoundingClientRect();
					const view = log.getBoundingClientRect();
					return { top: box.top, bottom: box.bottom, viewTop: view.top, viewBottom: view.bottom };
				});
				expect(shown.top < shown.viewBottom && shown.bottom > shown.viewTop, `the new reply is on screen: ${JSON.stringify(shown)}`);
				for (const text of texts) {
					expect((await page.locator('[data-not-sent="true"]').filter({ hasText: text }).count()) === 1, 'what was queued at Stop is a not-sent row');
				}
			}
		},
	],
	allow: [...DEV_INSTANCE_NOISE]
});
