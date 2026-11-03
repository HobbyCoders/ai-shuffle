// Journey: send a second message while the first reply is still streaming.
// The second message must show as queued/pending right away (the user can see
// it was taken), be delivered exactly once after the first turn, and get its
// own reply. At no painted frame may it show twice (queued bubble and
// delivered row together, or two rows), and nothing may remount.
import { fakeAiTag, runJourney } from './runner.mjs';
import {
	SEEDED,
	SLOW_REPLY,
	api,
	chatIdByToken,
	composer,
	countUserRows,
	expect,
	gotoApp,
	holdBootLoad,
	openFromSidebar,
	sidebarRow,
	PROJECT,
	chatState,
	openNewChat,
	pickAccess,
	pickAgent,
	scriptTag,
	scrollTranscriptTo,
	seededChatId,
	sendButton,
	showSidebar,
	stopButton,
	transcriptScroll,
	typeAndSend,
	uniq,
	waitChat,
	waitChatOpen,
	waitTurnSettled,
	watchUserText
} from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
import { STOP_SELECTOR, expectStopFollowsStreaming, markTime } from './lib/timeline.mjs';

// Hosted journeys build shared desktop UI only. Explicitly opt into the native
// phone shell locally, matching transcript-reader's --mobile-shell convention.
const mobileShell = process.argv.includes('--mobile-shell');

async function openStopChat(ctx) {
	if (!mobileShell || ctx.variant.viewport !== 'phone') return openNewChat(ctx);
	const { page, status } = ctx;
	await gotoApp(page, status, '/mobile/');
	await waitChat(page, { surface: 'new' }, { timeout: 90_000 });
	const hero = page.getByRole('region', { name: 'New session' });
	await hero.waitFor();
	await pickAgent(page, hero);
}

/**
 * Every painted frame from now until the returned function runs: where the row
 * being read sits and, per queued message, how many copies show and where. Rows
 * are read off the DOM, not the recorder's score, so a move under its threshold
 * still shows. Positions come from an IntersectionObserver made inside the frame:
 * its entry holds the geometry the browser computed last before painting, after
 * the app's own scroll for that frame. A read inside the frame comes too early and
 * a read after it can catch a fold that has not painted yet.
 */
async function watchStopFrames(page, texts) {
	await page.evaluate((texts) => {
		const log = document.querySelector('[role="log"][aria-label="Conversation messages"]');
		const logTop = log.getBoundingClientRect().top;
		const rows = () => [...log.querySelectorAll('.chat-column > .msg-virtualized')];
		const key = rows().find((el) => el.getBoundingClientRect().bottom > logTop + 1)?.dataset.transcriptKey;
		const watch = (window.__stopFrames = { frames: [], next: 0, running: true, startedAt: performance.now() });
		const slot = document.querySelector('.input-slot');
		const bubblesAndRows = () => [
			...document.querySelectorAll('.pending-messages [role="article"], [data-not-sent="true"]')
		];
		const frame = () => {
			if (!watch.running) return;
			const order = watch.next++;
			const seen = new IntersectionObserver((entries) => {
				seen.disconnect();
				const painted = new Map(entries.map((entry) => [entry.target, entry.boundingClientRect.top]));
				const topOf = (el) =>
					Math.round((painted.get(el) ?? el.getBoundingClientRect().top) * 10) / 10;
				const anchor = rows().find((el) => el.dataset.transcriptKey === key);
				watch.frames.push({
					order,
					at: Math.round(performance.now() - watch.startedAt),
					scrollTop: Math.round(log.scrollTop),
					anchorTop: anchor ? topOf(anchor) : null,
					replyHeight: rows().at(-1)?.getBoundingClientRect().height ?? null,
					composerTop: slot ? topOf(slot) : 0,
					messages: texts.map((text) => {
						const has = (el) => (el.textContent || '').includes(text);
						const bubbles = [...document.querySelectorAll('.pending-messages [role="article"]')].filter(has);
						const notSent = [...document.querySelectorAll('[data-not-sent="true"]')].filter(has);
						return {
							bubbles: bubbles.length,
							notSent: notSent.length,
							labelledNotSent: bubbles.filter((el) => el.dataset.queueStatus === 'stopped').length,
							top: topOf(bubbles[0] ?? notSent[0] ?? log)
						};
					})
				});
			});
			for (const el of [log, slot, ...rows(), ...bubblesAndRows()].filter(Boolean)) seen.observe(el);
			requestAnimationFrame(frame);
		};
		requestAnimationFrame(frame);
	}, texts);
	return () =>
		page.evaluate(() => {
			const watch = window.__stopFrames;
			watch.running = false;
			// After a slow frame the browser delivers several of these together, in no set order.
			return watch.frames.sort((a, b) => a.order - b.order);
		});
}

/** The phone shell has a drawer rather than the sidebar's show button. */
async function openChatFromTheList(page, title) {
	if (!new URL(page.url()).pathname.startsWith('/mobile/')) return openFromSidebar(page, title);
	await page.getByRole('button', { name: /^Open menu/ }).click();
	await sidebarRow(page, title).click();
}

/** One more line of the stopped reply can still arrive and nudge what is below it. */
const REPLY_LINE_PX = 30;

/** Measured on a slow phone: at most 0.017 when the follow lands in the same task, 0.073 to 0.095 when it does not. */
const BUBBLE_SHIFT_TOTAL = 0.05;

/**
 * The stopped turn settles 1.2s after it ends (settle-hold.ts). The reader once
 * moved at that moment, so watch well past it.
 */
const PAST_SETTLEMENT_MS = 1200 + 3000;

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

/** How long Stop waits for the server to confirm (message-sender.ts). */
const STOP_TIMEOUT_MS = 10_000;

const jumpToLatestButton = (page) => page.getByRole('button', { name: 'Jump to latest message' });

/** The composer's Stop: a background job's row has a Stop button of its own. */
const stopGenerating = (page) => page.getByRole('button', { name: 'Stop generating' });

/** The park-wait scenario: its last words before parking, and how long its job runs. */
const PARKED_REPLY = "I'll wait for it";
const PARK_JOB_MS = 20_000;

/** Stop asks first when background work is running, since it ends that too. */
async function stopEverything(page) {
	await stopGenerating(page).click();
	await page.getByRole('button', { name: 'Stop everything' }).click();
}

/** Send a slow turn and queue messages the server has confirmed, so only Stop can change them from here. */
async function queueDuringASlowTurn(page, mark, variant, queued, opening = `stream tok-${uniq()}`) {
	await mark('send');
	// A second turn in this chat: its reply, not the last one with the same words.
	const replies = page.getByText(SLOW_REPLY.start, { exact: false });
	const repliesBefore = await replies.count();
	await typeAndSend(page, `${opening} ${scriptTag(variant, 'slow', { tps: 2 })}`);
	await replies.nth(repliesBefore).waitFor({ timeout: 60_000 });
	await waitChat(page, { streaming: 'true' });

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

function expectEachOnceInEveryFrame(frames) {
	frames.forEach((frame, i) => {
		frame.messages.forEach((message, m) => {
			expect(message.bubbles + message.notSent === 1, `queued message ${m} shows exactly once in frame ${i}: ${JSON.stringify(message)}`);
		});
	});
}

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

/**
 * Lets a case make this page miss the end of a turn the server really did end.
 * Set before the page loads; it starts dropping once `withholdTurnEnd` is on,
 * and stays on across a reload until it is turned off.
 */
async function canWithholdTurnEnd(page) {
	await page.addInitScript(() => {
		const withheld = () => sessionStorage.getItem('journey-withhold-turn-end') === '1';
		const feedTurnEnd = /"(?:type|event_type)"\s*:\s*"turn\.(?:completed|aborted)"/;
		const socketTurnEnd = /"(?:type|event_type)"\s*:\s*"(?:stopped|interrupted|done|turn\.completed|turn\.aborted)"/;
		const realFetch = window.fetch;
		window.fetch = async (input, init) => {
			const res = await realFetch(input, init);
			if (!new Headers(init?.headers).get('accept')?.includes('text/event-stream') || !res.body) return res;
			const decoder = new TextDecoder();
			const encoder = new TextEncoder();
			let tail = '';
			const kept = res.body.pipeThrough(new TransformStream({
				transform(chunk, out) {
					tail += decoder.decode(chunk, { stream: true });
					const frames = tail.split(/\r?\n\r?\n/);
					tail = frames.pop();
					for (const frame of frames) {
						if (!(withheld() && feedTurnEnd.test(frame))) out.enqueue(encoder.encode(`${frame}\n\n`));
					}
				}
			}));
			return new Response(kept, { status: res.status, statusText: res.statusText, headers: res.headers });
		};
		const onmessage = Object.getOwnPropertyDescriptor(WebSocket.prototype, 'onmessage');
		Object.defineProperty(WebSocket.prototype, 'onmessage', {
			configurable: true,
			get: onmessage.get,
			set(handler) {
				onmessage.set.call(this, (event) => {
					if (!(withheld() && typeof event.data === 'string' && socketTurnEnd.test(event.data))) handler.call(this, event);
				});
			}
		});
	});
}

const withholdTurnEnd = (page, on) => page.evaluate((value) => sessionStorage.setItem('journey-withhold-turn-end', value), on ? '1' : '0');

/** The recorder's next step starts once it has seen the old page go. */
async function reloadThePage(page, mark, name) {
	await mark(`reload: ${name}`);
	await page.reload({ waitUntil: 'domcontentloaded' });
	await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => done()))));
	await mark(`reloaded: ${name}`);
}

const STOPPED_BUBBLE = '.pending-messages [data-queue-status="stopped"]';

await runJourney({
	name: 'queue-while-busy',
	recorder: { selectors: [STOP_SELECTOR, 'pending=.pending-messages [role="article"]'] },
	cases: [
		{
			name: 'queue',
			async setup(ctx) {
				await openNewChat(ctx);
				await ctx.page.waitForTimeout(800);
			},
			async act({ page, mark, variant, log }) {
				await mark('send the first');
				await typeAndSend(page, `first tok-${uniq()} ${scriptTag(variant, 'slow', { tps: 5 })}`);
				await page.getByText(SLOW_REPLY.start, { exact: false }).first().waitFor({ timeout: 60_000 });
				await waitChat(page, { streaming: 'true' });

				await mark('send while busy');
				const second = `tok-${uniq()}`;
				const seen = await watchUserText(page, second);
				const t0 = Date.now();
				await typeAndSend(page, `queued ${second} ${fakeAiTag(variant)}`.trim());
				// Taken at once: the message shows (queued, or already a row) within a moment.
				await page.waitForFunction(
					(n) =>
						[...document.querySelectorAll('[data-user-turn-id], .pending-messages [role="article"]')].some((el) =>
							(el.textContent || '').includes(n)
						),
					second,
					{ timeout: 3000, polling: 'raf' }
				);
				log(`second message on screen ${Date.now() - t0}ms after send`);

				// The first reply finishes, then the second is delivered and answered.
				await page.getByText(SLOW_REPLY.end, { exact: false }).first().waitFor({ timeout: 90_000 });
				await waitTurnSettled(page, { text: `You said: “queued ${second}`, messagesAtLeast: 4, timeout: 90_000 });
				const peak = await seen();
				expect(peak.max === 1, `the queued message showed ${peak.max} times at once (at ${peak.at}ms)`);
				expect((await countUserRows(page, second)) === 1, 'the queued message is delivered once');
				expect((await page.locator('.pending-messages [role="article"]').count()) === 0, 'no pending bubble left behind');
			},
			verify({ recording }) {
				expectStopFollowsStreaming(recording, { from: markTime(recording, 'send the first') });
			}
		},
		{
			// #863: a reader who scrolled up while a turn runs stays where they are
			// when the queued turn starts. Only a reader at the bottom follows it.
			name: 'queue-scrolled-up',
			async setup(ctx) {
				await openNewChat(ctx);
				await ctx.page.waitForTimeout(800);
				// Enough transcript above the live turn to scroll up into.
				await typeAndSend(ctx.page, `history tok-${uniq()} ${scriptTag(ctx.variant, 'long', { tps: 400 })}`);
				await waitTurnSettled(ctx.page, { text: 'Here is a long answer', messagesAtLeast: 2, timeout: 90_000 });
			},
			async act({ page, mark, variant, log }) {
				await mark('send the first');
				await typeAndSend(page, `first tok-${uniq()} ${scriptTag(variant, 'slow', { tps: 5 })}`);
				await page.getByText(SLOW_REPLY.start, { exact: false }).first().waitFor({ timeout: 60_000 });
				await waitChat(page, { streaming: 'true' });

				await mark('queue the second');
				const second = `tok-${uniq()}`;
				await typeAndSend(page, `queued ${second} ${fakeAiTag(variant)}`.trim());
				await page.locator('.pending-messages [role="article"]').filter({ hasText: second }).waitFor({ timeout: 5000 });

				await mark('scroll up to read');
				await scrollTranscriptTo(page, 0.15);
				const before = await transcriptScroll(page);
				expect(!before.atBottom && before.firstRow, `scrolled up while the turn runs (${JSON.stringify(before)})`);

				await mark('the queued turn starts');
				await page.locator('[data-user-turn-id]').filter({ hasText: second }).first().waitFor({ state: 'attached', timeout: 90_000 });
				await page.waitForTimeout(1500);
				const after = await transcriptScroll(page);
				log(`before ${JSON.stringify(before)}, after ${JSON.stringify(after)}`);
				// Rows can be taller than the screen, so the same first row alone is not
				// proof: the scroll offset must hold too (new content grows below it).
				expect(
					!after.atBottom && after.firstRow === before.firstRow && Math.abs(after.top - before.top) <= 150,
					`the queued turn starting moved the reader (before ${JSON.stringify(before)}, after ${JSON.stringify(after)})`
				);
				await waitTurnSettled(page, { text: `You said: “queued ${second}`, messagesAtLeast: 6, timeout: 90_000 });
			}
		},
		{
			// Stop aborts queued messages on purpose (they must not run), but the
			// words must survive: each one becomes its own "Not sent (stopped)" row
			// that stays across a reload and never touches what is in the composer,
			// and restoring one keeps whatever is already typed there.
			name: 'stop-keeps-queued-text',
			async setup(ctx) {
				await openNewChat(ctx);
				await ctx.page.waitForTimeout(800);
			},
			async act({ page, mark, variant, log }) {
				const first = `tok-${uniq()}`;
				const second = `tok-${uniq()}`;
				const draft = `half-typed tok-${uniq()}`;
				const notSent = (text) => page.locator('[data-not-sent="true"]').filter({ hasText: text });
				const composerText = () => page.locator('[contenteditable="true"]').first().innerText();

				await mark('send');
				await typeAndSend(page, `stream tok-${uniq()} ${scriptTag(variant, 'slow', { tps: 2 })}`);
				await page.getByText(SLOW_REPLY.start, { exact: false }).first().waitFor({ timeout: 60_000 });
				await waitChat(page, { streaming: 'true' });

				await mark('queue two');
				await typeAndSend(page, `queued ${first}`);
				await page.locator('.pending-messages [role="article"]').filter({ hasText: first }).waitFor({ timeout: 5000 });
				await typeAndSend(page, `queued ${second}`);
				await page.locator('.pending-messages [role="article"]').filter({ hasText: second }).waitFor({ timeout: 5000 });

				await mark('type a draft');
				await page.locator('[contenteditable="true"]').first().click();
				await page.keyboard.type(draft);

				await mark('stop');
				await stopButton(page).click();
				await waitChat(page, { streaming: 'false' }, { timeout: 15_000 });
				await stopButton(page).waitFor({ state: 'detached', timeout: 5000 });
				await notSent(first).waitFor({ timeout: 5000 });
				await notSent(second).waitFor({ timeout: 5000 });
				expect((await page.locator('.pending-messages [role="article"]').count()) === 0, 'no pending bubble left behind');
				expect((await composerText()).includes(draft), 'Stop left the draft in the composer alone');
				expect(!(await composerText()).includes(first), 'Stop did not write queued text into the composer');
				await page.getByText('[Stopped]', { exact: false }).first().waitFor({ timeout: 5000 });

				// Stay stopped: neither message may start a turn on its own.
				await page.waitForTimeout(3000);
				const state = await page.evaluate(() => document.querySelector('[data-ui-state="chat"]').getAttribute('data-ui-streaming'));
				expect(state === 'false', 'still not streaming 3s after Stop');
				expect((await page.getByText(`You said: “queued ${first}`, { exact: false }).count()) === 0, 'the queued message never ran');

				await mark('reload');
				await page.reload({ waitUntil: 'domcontentloaded' });
				// A new step once the recorder has seen the old page go: the reload
				// itself is not the app going backwards; what boots next is judged.
				await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => done()))));
				await mark('page reloaded');
				await notSent(first).waitFor({ timeout: 30_000 });
				await notSent(second).waitFor({ timeout: 5000 });
				await waitChat(page, { streaming: 'false' }, { timeout: 15_000 });
				await page.getByText('[Stopped]', { exact: false }).first().waitFor({ timeout: 5000 });
				// A not-sent row must not take over the viewport: the reply it follows
				// stays on screen with it after a reload.
				await page.waitForTimeout(500);
				const onScreen = await page.evaluate(() => {
					const box = document.querySelector('.msg-scroll').getBoundingClientRect();
					const stopped = [...document.querySelectorAll('.msg-scroll *')].find((el) => !el.children.length && el.textContent.includes('[Stopped]'));
					const top = stopped.getBoundingClientRect().top;
					return top >= box.top && top < box.bottom;
				});
				expect(onScreen, 'the [Stopped] reply is in view after reload');

				// A composer that already holds text keeps it when a row is restored.
				await page.locator('[contenteditable="true"]').first().click();
				await page.keyboard.type(draft);

				await mark('restore one');
				await notSent(first).getByRole('button', { name: 'Restore to composer' }).click();
				await notSent(first).waitFor({ state: 'detached', timeout: 10_000 });
				await page.waitForFunction((t) => document.querySelector('[contenteditable="true"]')?.innerText.includes(t), `queued ${first}`, { timeout: 10_000 });
				const merged = await composerText();
				log(`composer after restore: ${merged.replace(/\s+/g, ' ')}`);
				expect(merged.includes(draft), 'restoring kept the draft');
				expect((await notSent(second).count()) === 1, 'the other not-sent row is still there');

				await mark('reload after restore');
				await page.reload({ waitUntil: 'domcontentloaded' });
				await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => done()))));
				await mark('page reloaded after restore');
				await notSent(second).waitFor({ timeout: 30_000 });
				expect((await notSent(first).count()) === 0, 'a restored row stays gone after reload');
			},
			verify({ recording }) {
				expectStopFollowsStreaming(recording, { from: markTime(recording, 'send') });
			}
		},
		// Two rows fit under a pinned prompt, so the reader is still at the bottom.
		{ name: 'stop-holds-the-reader-at-the-bottom', ...stopReadingPosition({ scrolled: false, queued: 2 }) },
		// Three rows can run past a phone's fold. The settle pin keeps the prompt at
		// the top, as at the end of any reply, so what the reader sees must not move.
		{ name: 'stop-holds-the-reader-when-the-rows-overflow', ...stopReadingPosition({ scrolled: false, overflows: true }) },
		// More of them, Stop after Stop in one chat with no filler in between.
		{ name: 'stop-holds-the-reader-with-four-queued', ...stopReadingPosition({ scrolled: false, queued: 4, overflows: true, rounds: 2 }) },
		{ name: 'stop-holds-the-reader-scrolled-up', ...stopReadingPosition({ scrolled: true }) },
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
				// Fast enough to arrive in large pieces, slow enough to outlast typing two messages on a slow device.
				await typeAndSend(page, `stream tok-${uniq()} ${scriptTag(variant, 'long', { tps: 150 })}`);
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
		{
			// Stop reached the server and the rows came back, but this page never
			// hears the turn end. Stop's own timeout must not leave words in a
			// bubble with no way to get them back.
			name: 'stop-timeout-still-gives-the-words-back',
			async setup(ctx) {
				await canWithholdTurnEnd(ctx.page);
				await openStopChat(ctx);
				await ctx.page.waitForTimeout(800);
			},
			async act({ page, mark, variant, log }) {
				const texts = await queueDuringASlowTurn(page, mark, variant, 2);
				const notSent = (text) => page.locator('[data-not-sent="true"]').filter({ hasText: text });
				await withholdTurnEnd(page, true);
				const collect = await watchStopFrames(page, texts);
				await mark('stop');
				const pressedAt = Date.now();
				await stopButton(page).click();
				await page.waitForFunction((sel) => document.querySelectorAll(sel).length === 2, STOPPED_BUBBLE, { timeout: 8000 });
				log(`labelled bubbles at ${Date.now() - pressedAt}ms`);
				expect((await notSent(texts[0]).count()) === 0, 'the rows wait in bubbles while the turn looks live');
				await mark('timeout');
				await notSent(texts[0]).waitFor({ timeout: 15_000 });
				const waited = Date.now() - pressedAt;
				log(`rows at ${waited}ms`);
				expect(waited >= 8000, `the timeout released them, not the turn ending (${waited}ms)`);
				await waitChat(page, { streaming: 'false' }, { timeout: 5000 });
				const frames = await collect();
				expectEachOnceInEveryFrame(frames);
				await mark('restore');
				await notSent(texts[0]).getByRole('button', { name: 'Restore to composer' }).click();
				await page.waitForFunction((t) => document.querySelector('[role="textbox"][aria-label="Message"]')?.textContent?.includes(t), `queued ${texts[0]}`, { timeout: 10_000 });
				await notSent(texts[0]).waitFor({ state: 'detached', timeout: 10_000 });
				await notSent(texts[1]).getByRole('button', { name: 'Restore to composer' }).waitFor({ timeout: 5000 });
			}
		},
		{
			// Stop before the feed has named the turn: the queue's own socket empties
			// the bubbles at once, and the rows are behind the stalled feed. Each
			// message must still show the whole time.
			name: 'stop-before-the-turn-is-named-keeps-every-bubble',
			async setup(ctx) {
				await ctx.page.addInitScript(() => {
					const original = window.fetch;
					window.fetch = async (input, init) => {
						const url = typeof input === 'string' ? input : input.url;
						if (/\/sessions\/[^/]+\/events/.test(url)) await window.__feedHeld;
						return original(input, init);
					};
					window.__feedHeld = new Promise((resolve) => { window.__releaseFeed = resolve; });
				});
				await openStopChat(ctx);
				await ctx.page.waitForTimeout(800);
			},
			async act({ page, mark, variant, log }) {
				await mark('send');
				await typeAndSend(page, `stream tok-${uniq()} ${scriptTag(variant, 'slow', { tps: 2 })}`);
				await waitChat(page, { streaming: 'true' });
				await mark('queue');
				const texts = [`tok-${uniq()}`, `tok-${uniq()}`];
				for (const text of texts) {
					await typeAndSend(page, `queued ${text}`);
					await page.locator('.pending-messages [role="article"]').filter({ hasText: text }).waitFor({ timeout: 5000 });
				}
				await page.waitForFunction(
					(n) => document.querySelectorAll('.pending-messages [data-queue-confirmed="true"]').length === n,
					texts.length,
					{ timeout: 30_000 }
				);
				const collect = await watchStopFrames(page, texts);
				await mark('stop');
				await stopGenerating(page).click();
				// Long enough for the queue's socket to report it empty.
				await page.waitForTimeout(1500);
				await mark('the feed catches up');
				await page.evaluate(() => window.__releaseFeed());
				await waitChat(page, { streaming: 'false' }, { timeout: 30_000 });
				for (const text of texts) await page.locator('[data-not-sent="true"]').filter({ hasText: text }).waitFor({ timeout: 8000 });
				await page.waitForTimeout(500);
				const frames = await collect();
				log(`${frames.length} frames; last: ${JSON.stringify(frames.at(-1).messages)}`);
				expectEachOnceInEveryFrame(frames);
			}
		},
		{
			// The server takes the Stop but the page hears nothing back on either route,
			// so the timeout gives up waiting. Once it can hear again, the words are rows
			// that can be restored, and they showed exactly once all the way through.
			name: 'stop-confirmation-times-out-then-rows-can-be-restored',
			async setup(ctx) {
				await ctx.page.addInitScript(() => {
					const original = window.fetch;
					window.__deaf = null;
					window.fetch = async (input, init) => {
						const response = await original(input, init);
						const url = typeof input === 'string' ? input : input.url;
						if (!/\/sessions\/[^/]+\/events/.test(url) || !response.body) return response;
						const reader = response.body.getReader();
						const body = new ReadableStream({
							async pull(controller) {
								const { done, value } = await reader.read();
								if (window.__deaf) await window.__deaf;
								if (done) controller.close();
								else controller.enqueue(value);
							},
							cancel: (reason) => reader.cancel(reason)
						});
						return new Response(body, { status: response.status, headers: response.headers });
					};
					const Native = window.WebSocket;
					window.WebSocket = class extends Native {
						constructor(...args) {
							super(...args);
							this.addEventListener('message', (event) => {
								if (window.__deaf) event.stopImmediatePropagation();
							});
						}
					};
				});
				await openStopChat(ctx);
				await ctx.page.waitForTimeout(800);
			},
			async act({ page, mark, variant, log }) {
				const notSent = (text) => page.locator('[data-not-sent="true"]').filter({ hasText: text });
				const texts = await queueDuringASlowTurn(page, mark, variant, 2);
				await page.evaluate(() => {
					window.__deaf = new Promise((resolve) => { window.__hearAgain = resolve; });
				});
				const collect = await watchStopFrames(page, texts);
				await mark('stop');
				await stopGenerating(page).click();
				// Streaming ends on this page only because the timeout gave up.
				await waitChat(page, { streaming: 'false' }, { timeout: STOP_TIMEOUT_MS + 3000 });
				await page.waitForTimeout(500);
				const deaf = await collect();
				log(`at the timeout: ${JSON.stringify(deaf.at(-1).messages)}`);
				expectEachOnceInEveryFrame(deaf);

				await mark('the page hears again');
				const resume = await watchStopFrames(page, texts);
				await page.evaluate(() => { window.__deaf = null; window.__hearAgain(); });
				for (const text of texts) await notSent(text).waitFor({ timeout: 30_000 });
				await page.waitForTimeout(500);
				const heard = await resume();
				log(`after: ${JSON.stringify(heard.at(-1).messages)}`);
				expectEachOnceInEveryFrame(heard);
				for (const text of texts) {
					await notSent(text).getByRole('button', { name: 'Restore to composer' }).waitFor({ timeout: 5000 });
				}
				expect((await page.locator('.pending-messages [role="article"]').count()) === 0, 'no bubble is left behind');
				expect((await page.getByText(`You said: “queued`, { exact: false }).count()) === 0, 'neither message ran');
			}
		},
		{
			// The Stop never left: the server still has the turn and the queue. After
			// the timeout gives up waiting, nothing was refused, so nothing may read
			// "Not sent": the words stay queued and run when the turn ends.
			name: 'stop-that-never-arrives-leaves-the-queue-alone',
			async setup(ctx) {
				await ctx.page.addInitScript(() => {
					const send = WebSocket.prototype.send;
					WebSocket.prototype.send = function (data) {
						if (sessionStorage.getItem('journey-drop-stop') === '1' && typeof data === 'string' && /"type"\s*:\s*"stop"/.test(data)) return;
						return send.call(this, data);
					};
				});
				await openStopChat(ctx);
				await ctx.page.waitForTimeout(800);
			},
			async act({ page, mark, variant, log }) {
				const texts = await queueDuringASlowTurn(page, mark, variant, 1);
				await page.evaluate(() => sessionStorage.setItem('journey-drop-stop', '1'));
				const collect = await watchStopFrames(page, texts);
				await mark('stop');
				await stopGenerating(page).click();
				await page.waitForTimeout(STOP_TIMEOUT_MS + 1000);
				const atTimeout = await collect();
				const afterTimeout = atTimeout.at(-1).messages;
				log(`after the timeout: ${JSON.stringify(afterTimeout)}`);
				expectEachOnceInEveryFrame(atTimeout);
				atTimeout.forEach((frame, i) => frame.messages.forEach((message, m) => {
					expect(message.labelledNotSent === 0 && message.notSent === 0, `queued message ${m} reads "Not sent" in frame ${i} (${frame.at}ms)`);
				}));

				await mark('the turn ends by itself');
				for (const text of texts) {
					await page.getByText(`queued ${text}`, { exact: false }).first().waitFor({ timeout: 90_000 });
				}
				await page.getByText('You said', { exact: false }).first().waitFor({ timeout: 120_000 });
				await waitChat(page, { streaming: 'false' }, { timeout: 120_000 });
				expect((await page.locator('[data-not-sent="true"]').count()) === 0, 'nothing became a not-sent row');
			}
		},
		{
			// Stop also ends the chat's background work, which must not bring back a
			// wake turn that sends what Stop refused.
			name: 'stop-with-a-background-job-keeps-the-words-out',
			async setup(ctx) {
				await openNewChat(ctx, { project: PROJECT });
				await pickAccess(ctx.page, 'Bypass');
				await ctx.page.waitForTimeout(800);
			},
			async act({ page, mark, variant, log }) {
				const notSent = (text) => page.locator('[data-not-sent="true"]').filter({ hasText: text });
				await mark('start a background job');
				await typeAndSend(page, `job tok-${uniq()} ${scriptTag(variant, 'tool-background-stream', { tps: 2, latency_ms: 0 })}`);
				await page.locator('[data-ui-mount="MessageArea"]').getByTestId('tasks-toggle').waitFor({ timeout: 60_000 });
				await waitChat(page, { streaming: 'true' }, { timeout: 30_000 });

				await mark('queue');
				const texts = [`tok-${uniq()}`, `tok-${uniq()}`];
				for (const text of texts) {
					await typeAndSend(page, `queued ${text}`);
					await page.locator('.pending-messages [role="article"]').filter({ hasText: text }).waitFor({ timeout: 5000 });
				}
				const collect = await watchStopFrames(page, texts);
				await mark('stop');
				await stopEverything(page);
				await waitChat(page, { streaming: 'false' }, { timeout: 15_000 });
				for (const text of texts) await notSent(text).waitFor({ timeout: 8000 });
				await page.waitForTimeout(PAST_SETTLEMENT_MS);
				const frames = await collect();
				expectEachOnceInEveryFrame(frames);
				const state = await chatState(page);
				log(`after Stop: ${JSON.stringify(state)}`);
				expect(state.streaming === 'false', 'still stopped well after Stop');
				expect((await page.getByText(`You said: “queued`, { exact: false }).count()) === 0, 'neither message ran');
				await notSent(texts[0]).getByRole('button', { name: 'Restore to composer' }).waitFor({ timeout: 5000 });
			}
		},
		{
			// Parked on a job, Stop is still offered, and ends the job: the wake it was
			// waiting for must not come, and there is no turn left to jump around.
			name: 'stop-on-a-parked-turn-ends-the-job',
			async setup(ctx) {
				await openNewChat(ctx, { project: PROJECT });
				await pickAccess(ctx.page, 'Bypass');
				await ctx.page.waitForTimeout(800);
			},
			async act({ page, mark, variant, log }) {
				await mark('park');
				await typeAndSend(page, `park tok-${uniq()} ${scriptTag(variant, 'park-wait')}`);
				await page.getByText(PARKED_REPLY, { exact: false }).first().waitFor({ timeout: 60_000 });
				await waitChat(page, { streaming: 'false' }, { timeout: 30_000 });

				await mark('stop while parked');
				await stopEverything(page);
				await stopGenerating(page).waitFor({ state: 'detached', timeout: 15_000 });
				await page.waitForTimeout(1000);
				const before = await chatState(page);

				await mark('past the job');
				await page.waitForTimeout(PARK_JOB_MS + 5000);
				const after = await chatState(page);
				log(`after Stop ${JSON.stringify(before)}; past the job ${JSON.stringify(after)}`);
				expect(after.streaming === 'false', 'stayed stopped past the job');
				expect(after.messages === before.messages, `the job did not wake the chat (${before.messages} → ${after.messages} messages)`);
			}
		},
		{
			// Parked on a job, a new turn with messages queued, then Stop: the job's
			// wake must not run what Stop refused.
			name: 'stop-after-a-parked-turn-keeps-the-words-out',
			async setup(ctx) {
				await openNewChat(ctx, { project: PROJECT });
				await pickAccess(ctx.page, 'Bypass');
				await ctx.page.waitForTimeout(800);
			},
			async act({ page, mark, variant }) {
				const notSent = (text) => page.locator('[data-not-sent="true"]').filter({ hasText: text });
				await mark('park');
				await typeAndSend(page, `park tok-${uniq()} ${scriptTag(variant, 'park-wait')}`);
				await page.getByText(PARKED_REPLY, { exact: false }).first().waitFor({ timeout: 60_000 });
				await waitChat(page, { streaming: 'false' }, { timeout: 30_000 });

				const texts = await queueDuringASlowTurn(page, mark, variant, 2);
				const collect = await watchStopFrames(page, texts);
				await mark('stop');
				await stopEverything(page);
				await waitChat(page, { streaming: 'false' }, { timeout: 15_000 });
				for (const text of texts) await notSent(text).waitFor({ timeout: 8000 });

				await mark('past the job');
				await page.waitForTimeout(PARK_JOB_MS + 5000);
				const frames = await collect();
				expectEachOnceInEveryFrame(frames);
				expect((await page.getByText(`You said: “queued`, { exact: false }).count()) === 0, 'neither message ran');
				for (const text of texts) expect((await notSent(text).count()) === 1, 'each is still one not-sent row');
			}
		},
		{
			// A reload while the turn still looks live rebuilds the same hold from the
			// replayed events: bubbles again, and rows once the turn has ended.
			name: 'stop-reload-before-the-turn-ends-shows-bubbles',
			async setup(ctx) {
				await canWithholdTurnEnd(ctx.page);
				await openStopChat(ctx);
				await ctx.page.waitForTimeout(800);
			},
			async act({ page, mark, variant, log }) {
				const texts = await queueDuringASlowTurn(page, mark, variant, 2);
				const notSent = page.locator('[data-not-sent="true"]');
				await withholdTurnEnd(page, true);
				await mark('stop');
				await stopButton(page).click();
				await page.waitForFunction((sel) => document.querySelectorAll(sel).length === 2, STOPPED_BUBBLE, { timeout: 8000 });

				await reloadThePage(page, mark, 'turn still looks live');
				await waitChat(page, { surface: 'active', loading: 'false', session: /^s:/, messagesAtLeast: 1 }, { timeout: 60_000 });
				await page.waitForFunction((sel) => document.querySelectorAll(sel).length === 2, STOPPED_BUBBLE, { timeout: 30_000 });
				await page.waitForTimeout(1500);
				const shown = await page.locator(STOPPED_BUBBLE).allInnerTexts();
				log(`after the reload: ${shown.length} labelled bubbles, ${await notSent.count()} rows`);
				expect(texts.every((t) => shown.filter((s) => s.includes(t)).length === 1), `each is one bubble: ${shown.join(' | ')}`);
				expect((await notSent.count()) === 0, 'no row beside a bubble');

				await withholdTurnEnd(page, false);
				await reloadThePage(page, mark, 'turn ended');
				for (const text of texts) {
					await notSent.filter({ hasText: text }).getByRole('button', { name: 'Restore to composer' }).waitFor({ timeout: 30_000 });
				}
				expect((await page.locator(STOPPED_BUBBLE).count()) === 0, 'no bubble beside a row');
			}
		},
		{
			// The usual reload, with no help: by the time the page is back the turn
			// has ended, so the words come back as rows that can be restored.
			name: 'stop-reload-right-away-shows-rows',
			async setup(ctx) {
				await openStopChat(ctx);
				await ctx.page.waitForTimeout(800);
			},
			async act({ page, mark, variant, log }) {
				const texts = await queueDuringASlowTurn(page, mark, variant, 2);
				const notSent = (text) => page.locator('[data-not-sent="true"]').filter({ hasText: text });
				await mark('stop');
				await stopButton(page).click();
				await reloadThePage(page, mark, 'right after Stop');
				await page.locator('[role="log"][aria-label="Conversation messages"]').waitFor({ timeout: 60_000 });
				const collect = await watchStopFrames(page, texts);
				for (const text of texts) {
					await notSent(text).getByRole('button', { name: 'Restore to composer' }).waitFor({ timeout: 30_000 });
				}
				await waitChat(page, { streaming: 'false' }, { timeout: 15_000 });
				await page.waitForTimeout(1500);
				const frames = await collect();
				frames.forEach((frame, i) => frame.messages.forEach((m) => {
					expect(m.bubbles + m.notSent <= 1, `shown twice in frame ${i}: ${JSON.stringify(m)}`);
				}));
				const asBubble = frames.filter((f) => f.messages.some((m) => m.bubbles)).length;
				log(`after the reload: ${asBubble} of ${frames.length} frames showed a bubble before the rows`);
				expect((await page.locator(STOPPED_BUBBLE).count()) === 0, 'no bubble left beside a row');
			}
		},
		{
			// Stop, leave straight away, come back. Stop and the switch are two
			// actions, so two steps: the chat gaining its rows just before it is left
			// is not the chat going backwards.
			name: 'stop-then-switch-away-and-back-keeps-the-rows',
			async setup(ctx) {
				await openStopChat(ctx);
				await ctx.page.waitForTimeout(800);
			},
			async act({ page, mark, variant }) {
				const opening = `stream tok-${uniq()}`;
				const texts = await queueDuringASlowTurn(page, mark, variant, 2, opening);
				const notSent = (text) => page.locator('[data-not-sent="true"]').filter({ hasText: text });
				const { title } = await api(page, 'GET', `/sessions/${await chatIdByToken(page, opening)}`);
				await mark('stop');
				await stopButton(page).click();
				// The chat counts the rows the moment the server writes them, about 100ms
				// after Stop and well inside the hold. Leaving before that would count as
				// 2, 4, 2 messages across two different chats, which is not this chat going
				// backwards. That order behaves the same on main.
				await waitChat(page, { messagesAtLeast: 4 }, { timeout: 5000 });
				await mark('leave');
				await openChatFromTheList(page, SEEDED.no_project);
				await waitChatOpen(page, { messagesAtLeast: 2, hold: 500 });
				await mark('come back');
				const collect = await watchStopFrames(page, texts);
				await openChatFromTheList(page, title);
				for (const text of texts) {
					await notSent(text).getByRole('button', { name: 'Restore to composer' }).waitFor({ timeout: 15_000 });
				}
				await waitChat(page, { streaming: 'false' }, { timeout: 15_000 });
				await page.waitForTimeout(1500);
				const frames = await collect();
				frames.forEach((frame, i) => frame.messages.forEach((m) => {
					expect(m.bubbles + m.notSent <= 1, `shown twice in frame ${i}: ${JSON.stringify(m)}`);
				}));
				for (const text of texts) expect((await notSent(text).count()) === 1, `one row for "${text}"`);
				expect((await page.locator(STOPPED_BUBBLE).count()) === 0, 'no bubble left beside a row');
			}
		},
		{
			// The server drops a restored row whether or not this page hears back, so
			// the words must reach this chat's composer in every one of these cases.
			name: 'restore-survives-a-chat-switch-and-a-lost-reply',
			allow: [{ kind: 'failed_requests', url: /\/commands\/rewind\/execute\//, reason: 'this case cuts the connection to a restore on purpose' }],
			async setup(ctx) {
				await openNewChat(ctx);
				await ctx.page.waitForTimeout(800);
			},
			async act({ page, mark, variant }) {
				const opening = `opening tok-${uniq()}`;
				const switched = `tok-${uniq()}`;
				const lost = `tok-${uniq()}`;
				const reloaded = `tok-${uniq()}`;
				const unheard = `tok-${uniq()}`;
				const notSent = (text) => page.locator('[data-not-sent="true"]').filter({ hasText: text });
				const restoreButton = (text) => notSent(text).getByRole('button', { name: /^Restore/ });
				const composerHas = (text) => page.waitForFunction(
					(t) => document.querySelector('[contenteditable="true"]')?.innerText.includes(t), text, { timeout: 15_000 });
				const REWIND = '**/rewind/execute/**';

				await mark('send');
				await typeAndSend(page, `${opening} ${scriptTag(variant, 'slow', { tps: 2 })}`);
				await page.getByText(SLOW_REPLY.start, { exact: false }).first().waitFor({ timeout: 60_000 });
				await waitChat(page, { streaming: 'true' });
				const { title } = await api(page, 'GET', `/sessions/${await chatIdByToken(page, opening)}`);
				for (const words of [switched, lost, reloaded, unheard]) {
					await typeAndSend(page, `queued ${words}`);
					await page.locator('.pending-messages [role="article"]').filter({ hasText: words }).waitFor({ timeout: 5000 });
				}
				await mark('stop');
				await stopButton(page).click();
				await notSent(unheard).waitFor({ timeout: 15_000 });

				await mark('restore and leave before the reply');
				let release;
				const held = new Promise((done) => { release = done; });
				await page.route(REWIND, async (route) => { await held; await route.continue(); });
				await restoreButton(switched).click();
				await page.waitForFunction(() => [...document.querySelectorAll('[data-not-sent="true"] button')].some((b) => b.textContent.includes('Restoring')));
				await openFromSidebar(page, SEEDED.no_project);
				await waitChatOpen(page, { messagesAtLeast: 2, hold: 500 });
				const elsewhere = page.waitForResponse(REWIND);
				release();
				await elsewhere;
				await page.unroute(REWIND);
				expect(!(await page.locator('[contenteditable="true"]').first().innerText()).includes(switched),
					'the words did not land in the chat on screen');

				await mark('back to the first chat');
				await openFromSidebar(page, title);
				await notSent(lost).waitFor({ timeout: 30_000 });
				await composerHas(`queued ${switched}`);
				await notSent(switched).waitFor({ state: 'detached', timeout: 15_000 });

				await mark('restore with a lost reply');
				await page.route(REWIND, async (route) => { await route.fetch(); await route.abort('connectionreset'); });
				await restoreButton(lost).click();
				await composerHas(`queued ${lost}`);
				await page.unroute(REWIND);
				await notSent(lost).waitFor({ state: 'detached', timeout: 15_000 });
				const composed = await page.locator('[contenteditable="true"]').first().innerText();
				expect(composed.includes(`queued ${switched}`), 'the earlier restored words are still in the composer');
				expect(composed.split(`queued ${lost}`).length === 2, 'the words arrived once');

				await mark('reload while the answer is still on its way');
				await page.route(REWIND, async (route) => {
					await route.fetch();
					await new Promise((done) => setTimeout(done, 60_000));
				});
				await restoreButton(reloaded).click();
				await page.waitForTimeout(1500);
				// Removing the route can deliver the held reply. Reload first so this
				// page really leaves before the restore answers, as this step requires.
				await page.reload({ waitUntil: 'domcontentloaded' });
				await page.unrouteAll({ behavior: 'ignoreErrors' });
				await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => done()))));
				await mark('page reloaded');
				await notSent(unheard).waitFor({ timeout: 60_000 });
				await composerHas(`queued ${reloaded}`);
				expect((await notSent(reloaded).count()) === 0, 'the row the server dropped stayed gone');
				expect((await notSent(unheard).getByRole('button', { name: 'Restore to composer' }).count()) === 1, 'the other row was left alone');
				await page.waitForTimeout(1500);
				const afterReload = await page.locator('[contenteditable="true"]').first().innerText();
				expect(afterReload.split(`queued ${reloaded}`).length === 2, 'the words came back once');
				expect(afterReload.split(`queued ${lost}`).length <= 2, 'the earlier lost reply did not bring its words back again');

				await mark('restore the server never heard');
				await page.route(REWIND, (route) => route.abort('connectionreset'));
				await restoreButton(unheard).click();
				await composerHas(`queued ${unheard}`);
				await page.unroute(REWIND);
				await notSent(unheard).getByRole('alert').filter({ hasText: "Couldn't confirm the restore" }).waitFor({ timeout: 5000 });
				expect(await notSent(unheard).getByRole('button', { name: 'Restored' }).isDisabled(), 'a second click cannot copy the words again');
			}
		},
		{
			// #1031: a restore is already in the chat's saved draft by the time it
			// is done, so a reload has to bring it back, once, with whatever else was
			// in the composer. Boot is held so the composer always saves its empty
			// first state before boot says which chat this tab was on, the order that
			// used to wipe the draft. The Stop that stages the rows is unrecorded here:
			// stop-keeps-queued-text and the case above record it.
			name: 'restored-words-survive-a-reload-after-the-answer',
			allow: [{ kind: 'failed_requests', url: /\/commands\/rewind\/execute\//, reason: 'this case cuts the connection to a restore on purpose' }],
			async setup(ctx) {
				const { page, variant } = ctx;
				await openNewChat(ctx);
				await page.waitForTimeout(800);
				ctx.rows = { lost: `tok-${uniq()}`, answered: `tok-${uniq()}`, unheard: `tok-${uniq()}` };
				await typeAndSend(page, `opening tok-${uniq()} ${scriptTag(variant, 'slow', { tps: 2 })}`);
				await page.getByText(SLOW_REPLY.start, { exact: false }).first().waitFor({ timeout: 60_000 });
				await waitChat(page, { streaming: 'true' });
				for (const words of [ctx.rows.lost, ctx.rows.answered, ctx.rows.unheard]) {
					await typeAndSend(page, `queued ${words}`);
					await page.locator('.pending-messages [role="article"]').filter({ hasText: words }).waitFor({ timeout: 5000 });
				}
				await stopButton(page).click();
				await page.locator('[data-not-sent="true"]').filter({ hasText: ctx.rows.unheard }).waitFor({ timeout: 15_000 });
				await holdBootLoad(page, '/api/v1/preferences/all', 30_000, { releaseOnSignal: true });
			},
			async act({ page, mark, rows: { lost, answered, unheard } }) {
				const duringBoot = `mid-boot tok-${uniq()}`;
				const typed = `half-typed tok-${uniq()}`;
				const notSent = (text) => page.locator('[data-not-sent="true"]').filter({ hasText: text });
				const restoreButton = (text) => notSent(text).getByRole('button', { name: /^Restore/ });
				const editor = () => page.locator('[contenteditable="true"]').first();
				const composerHas = (text) => page.waitForFunction(
					(t) => document.querySelector('[contenteditable="true"]')?.innerText.includes(t), text, { timeout: 15_000 });
				const expectOnce = async (words) => {
					const composed = await editor().innerText();
					for (const text of words) expect(composed.split(text).length === 2, `"${text}" is in the composer once (${composed.replace(/\s+/g, ' ')})`);
				};
				// The composer is up and past its 100 ms draft save, and boot has not yet
				// said which chat it is on.
				const reloadWithBootHeld = async (name) => {
					await page.reload({ waitUntil: 'domcontentloaded' });
					await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => done()))));
					await mark(name);
					await editor().waitFor({ timeout: 60_000 });
					await page.waitForFunction(() => typeof window.__journeyBootHold?.release === 'function', null, { timeout: 60_000 });
					await page.waitForTimeout(400);
				};
				const finishBoot = async () => {
					await page.evaluate(() => window.__journeyBootHold.release());
					await notSent(unheard).waitFor({ timeout: 60_000 });
				};

				await mark('restore with a lost answer, reload at once, and type during boot');
				const REWIND = '**/rewind/execute/**';
				await page.route(REWIND, async (route) => { await route.fetch(); await route.abort('connectionreset'); });
				await restoreButton(lost).click();
				await composerHas(`queued ${lost}`);
				await reloadWithBootHeld('page reloaded after the lost answer');
				await page.unroute(REWIND);
				await editor().click();
				await page.keyboard.type(duringBoot);
				await finishBoot();
				await composerHas(`queued ${lost}`);
				await composerHas(duringBoot);
				// Long enough for a second hand-back to have shown.
				await page.waitForTimeout(1500);
				await expectOnce([`queued ${lost}`, duringBoot]);
				expect((await notSent(lost).count()) === 0, 'the lost-answer row stayed gone');

				await mark('restore over a draft, and reload as the answer lands');
				await editor().click();
				// The click lands mid-text once the words wrap on a phone.
				await page.keyboard.press('Control+End');
				await page.keyboard.type(` ${typed}`);
				await restoreButton(answered).click();
				await composerHas(`queued ${answered}`);
				await reloadWithBootHeld('page reloaded after the answer');
				await finishBoot();
				await composerHas(`queued ${answered}`);
				await composerHas(typed);
				await page.waitForTimeout(1500);
				await expectOnce([`queued ${lost}`, `queued ${answered}`, duringBoot, typed]);
				expect((await notSent(answered).count()) === 0, 'the answered row stayed gone');
			}
		},
		{
			name: 'restore-finishing-with-no-composer-on-screen',
			allow: [{ kind: 'failed_requests', url: /\/commands\/rewind\/execute\//, reason: 'this case cuts the connection to a restore on purpose' }],
			async setup(ctx) {
				await openNewChat(ctx);
				await ctx.page.waitForTimeout(800);
			},
			async act({ page, mark, variant }) {
				const opening = `opening tok-${uniq()}`;
				const answered = `tok-${uniq()}`;
				const departed = `tok-${uniq()}`;
				const notSent = (text) => page.locator('[data-not-sent="true"]').filter({ hasText: text });
				const composerHas = (text) => page.waitForFunction(
					(t) => document.querySelector('[contenteditable="true"]')?.innerText.includes(t), text, { timeout: 15_000 });
				const REWIND = '**/rewind/execute/**';
				const openSettings = async () => {
					const sessions = await showSidebar(page);
					await sessions.getByRole('navigation', { name: 'Workspace' }).getByRole('button', { name: 'Settings', exact: true }).click();
					await page.getByRole('navigation', { name: 'Settings' }).waitFor({ timeout: 15_000 });
				};
				// The server commits, then the answer is held until released and lost.
				const holdAnswer = async () => {
					let release;
					const held = new Promise((done) => { release = done; });
					const committed = new Promise((done) => {
						page.route(REWIND, async (route) => {
							await route.fetch();
							done();
							await held;
							await route.abort('connectionreset');
						});
					});
					return { release, committed };
				};
				const restoreBehindSettings = async (words, beforeRelease) => {
					const lost = page.waitForEvent('requestfailed', (r) => r.url().includes('/rewind/execute/'));
					const { release, committed } = await holdAnswer();
					await notSent(words).getByRole('button', { name: /^Restore/ }).click();
					await committed;
					await openSettings();
					expect((await page.locator('[contenteditable="true"]').count()) === 0, 'no composer is on screen');
					await beforeRelease?.();
					release();
					await lost;
					await page.unrouteAll({ behavior: 'ignoreErrors' });
				};

				await mark('send');
				await typeAndSend(page, `${opening} ${scriptTag(variant, 'slow', { tps: 2 })}`);
				await page.getByText(SLOW_REPLY.start, { exact: false }).first().waitFor({ timeout: 60_000 });
				await waitChat(page, { streaming: 'true' });
				const { title } = await api(page, 'GET', `/sessions/${await chatIdByToken(page, opening)}`);
				for (const words of [answered, departed]) {
					await typeAndSend(page, `queued ${words}`);
					await page.locator('.pending-messages [role="article"]').filter({ hasText: words }).waitFor({ timeout: 5000 });
				}
				await mark('stop');
				await stopButton(page).click();
				await notSent(departed).waitFor({ timeout: 15_000 });

				await mark('the answer is lost while Settings is open');
				await restoreBehindSettings(answered);
				await mark('another chat and back');
				await page.waitForTimeout(300);
				await openFromSidebar(page, SEEDED.no_project);
				await waitChatOpen(page, { messagesAtLeast: 2, hold: 500 });
				await openFromSidebar(page, title);
				await notSent(departed).waitFor({ timeout: 30_000 });
				await composerHas(`queued ${answered}`);

				await mark('same, with the departure cancelled first');
				await restoreBehindSettings(departed, () => page.evaluate(() => window.dispatchEvent(new Event('beforeunload'))));
				// Past the wait for the page to go, when the words are handed over.
				await page.waitForTimeout(1500);
				await mark('another chat and back');
				await openFromSidebar(page, SEEDED.no_project);
				await waitChatOpen(page, { messagesAtLeast: 2, hold: 500 });
				await openFromSidebar(page, title);
				await composerHas(`queued ${departed}`);
				await notSent(departed).waitFor({ state: 'detached', timeout: 30_000 });
				const composed = await page.locator('[contenteditable="true"]').first().innerText();
				expect(composed.split(`queued ${answered}`).length === 2, 'the first words arrived once');
				expect(composed.split(`queued ${departed}`).length === 2, 'the second words arrived once');
				expect(await page.evaluate(() => !Object.keys(sessionStorage).some((k) => k.startsWith('restore-note:'))), 'no note is left over');
			}
		},
		{
			// A note is planted here, because a chat cannot be stopped down to a
			// single not-sent row from the UI. What it proves is the next page's
			// side: a chat with no rows at all, whose layout has no transcript.
			name: 'reload-after-restoring-the-only-row',
			async setup(ctx) {
				await openNewChat(ctx);
				await ctx.page.waitForTimeout(800);
			},
			async act({ page, mark, status }) {
				const words = `only row tok-${uniq()}`;
				const chatId = seededChatId(status, 'cleared');
				const composer = page.locator('[contenteditable="true"]').first();

				await mark('open the empty chat');
				await openFromSidebar(page, SEEDED.cleared);
				await waitChat(page, { surface: 'cleared', loading: 'false' });
				await page.evaluate(([id, text]) => sessionStorage.setItem(`restore-note:${id}:only-row`,
					JSON.stringify({ rowId: 'only-row', prompt: text, uploadPaths: [], page: 'an-earlier-page' })), [chatId, words]);

				await mark('reload');
				await page.reload({ waitUntil: 'domcontentloaded' });
				await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => done()))));
				await mark('page reloaded');
				await page.waitForFunction(
					(t) => document.querySelector('[contenteditable="true"]')?.innerText.includes(t), words, { timeout: 30_000 });
				await waitChat(page, { surface: 'cleared', loading: 'false' });
				await page.waitForTimeout(1500);
				expect((await composer.innerText()).split(words).length === 2, 'the words came back once');
				expect(await page.evaluate((id) => sessionStorage.getItem(`restore-note:${id}:only-row`) === null, chatId), 'the note is used up');

				// This is a shared seeded chat: leave its composer as it was found.
				await mark('clear the composer');
				await composer.click();
				await page.keyboard.press('ControlOrMeta+A');
				await page.keyboard.press('Backspace');
				await page.waitForTimeout(1000);
			}
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
});
