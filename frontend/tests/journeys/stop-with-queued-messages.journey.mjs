// Journey: the ways Stop with messages queued can go. A Stop the server never
// confirms, or that never leaves the page; a turn the feed has not named yet; a
// parked turn and its background job; a reload or a chat switch straight after.
// Words stay shown exactly once and can always be got back.
import { runJourney } from './runner.mjs';
import {
	SEEDED,
	api,
	chatIdByToken,
	composer,
	expect,
	openFromSidebar,
	sidebarRow,
	PROJECT,
	chatState,
	openNewChat,
	pickAccess,
	scriptTag,
	sendButton,
	stopButton,
	typeAndSend,
	uniq,
	waitChat,
	waitChatOpen
} from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
import { STOP_SELECTOR } from './lib/timeline.mjs';
import { openStopChat, watchStopFrames, PAST_SETTLEMENT_MS, queueDuringASlowTurn, expectEachOnceInEveryFrame } from './lib/stop-queue.mjs';

/** The phone shell has a drawer rather than the sidebar's show button. */
async function openChatFromTheList(page, title) {
	if (!new URL(page.url()).pathname.startsWith('/mobile/')) return openFromSidebar(page, title);
	await page.getByRole('button', { name: /^Open menu/ }).click();
	await sidebarRow(page, title).click();
}

/** How long Stop waits for the server to confirm (message-sender.ts). */
const STOP_TIMEOUT_MS = 10_000;

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

/**
 * Lets a case make this page miss the end of a turn the server really did end.
 * Set before the page loads; it starts dropping once `withholdTurnEnd` is on,
 * and stays on across a reload until it is turned off.
 */
async function canWithholdTurnEnd(page) {
	await page.addInitScript(() => {
		const withheld = () => sessionStorage.getItem('journey-withhold-turn-end') === '1';
		const held = [];
		window.__releaseHeldTurnEnds = () => held.splice(0).forEach((release) => release());
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
						const release = () => out.enqueue(encoder.encode(`${frame}\n\n`));
						if (withheld() && feedTurnEnd.test(frame)) held.push(release);
						else release();
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
					const release = () => handler.call(this, event);
					if (withheld() && typeof event.data === 'string' && socketTurnEnd.test(event.data)) held.push(release);
					else release();
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

/** A timeout must not change the next Stop, here or after navigating to another chat. */
function stopAfterTimeout({ switchChat = false } = {}) {
	return {
		async setup(ctx) {
			await canWithholdTurnEnd(ctx.page);
			await openStopChat(ctx);
			await ctx.page.waitForTimeout(800);
		},
		async act({ page, mark, variant, log }) {
			const timedOut = await queueDuringASlowTurn(page, mark, variant, 2);
			const notSent = (text) => page.locator('[data-not-sent="true"]').filter({ hasText: text });
			await withholdTurnEnd(page, true);
			await mark('first Stop times out');
			await stopGenerating(page).click();
			await waitChat(page, { streaming: 'false' }, { timeout: STOP_TIMEOUT_MS + 3000 });
			for (const text of timedOut) await notSent(text).getByRole('button', { name: 'Restore to composer' }).waitFor({ timeout: 5000 });
			await withholdTurnEnd(page, false);
			// Keep the feed's terminal boundary before starting the next turn; permanently
			// dropping it would test a different, incomplete transcript instead.
			await page.evaluate(() => window.__releaseHeldTurnEnds());

			if (switchChat) {
				await mark('switch chats after the timeout');
				await openChatFromTheList(page, SEEDED.no_project);
				await waitChatOpen(page, { messagesAtLeast: 2, hold: 500 });
			}

			// The previous reply can leave the virtual window as the new one grows;
			// count projected messages, not matching text nodes, to identify this reply.
			const before = Number((await chatState(page)).messages);
			await mark('send the later turn');
			await typeAndSend(page, `stream tok-${uniq()} ${scriptTag(variant, 'slow', { tps: 2 })}`);
			await waitChat(page, { messagesAtLeast: before + 2, streaming: 'true' });
			const texts = [`tok-${uniq()}`, `tok-${uniq()}`];
			await mark('queue for the later Stop');
			for (const text of texts) {
				await typeAndSend(page, `queued ${text}`);
				await page.locator('.pending-messages [role="article"]').filter({ hasText: text }).waitFor({ timeout: 5000 });
			}
			await page.waitForFunction((n) => document.querySelectorAll('.pending-messages [data-queue-confirmed="true"]').length === n, texts.length, { timeout: 30_000 });
			const fresh = `tok-${uniq()}`;
			await composer(page).click();
			await composer(page).fill(`fresh ${fresh} ${scriptTag(variant, 'default')}`);
			const collect = await watchStopFrames(page, [...texts, fresh]);
			await mark('a later Stop, immediately followed by Send');
			await stopGenerating(page).click();
			await sendButton(page).click();
			await page.getByText(`You said: “fresh ${fresh}`, { exact: false }).first().waitFor({ timeout: 60_000 });
			await waitChat(page, { streaming: 'false' }, { timeout: 60_000 });
			await page.waitForTimeout(500);
			const frames = await collect();
			const waiting = frames.filter((f) => f.messages[2].bubbles > 0);
			log(`after ${switchChat ? 'a chat switch and ' : ''}the earlier timeout: the new message waited in ${waiting.length} of ${frames.length} frames`);
			expect(waiting.length > 0, 'the immediate new send was shown in the queue');
			for (const frame of waiting) {
				frame.messages.slice(0, 2).forEach((message) => {
					expect(message.notSent === 0, `the later Stop kept its rows as bubbles while the new message waited: ${JSON.stringify(frame)}`);
				});
				expect(frame.messages[2].labelledNotSent === 0, 'the new message was not part of either Stop');
			}
			for (const text of texts) await notSent(text).getByRole('button', { name: 'Restore to composer' }).waitFor({ timeout: 5000 });
		}
	};
}

await runJourney({
	name: 'stop-with-queued-messages',
	recorder: { selectors: [STOP_SELECTOR, 'pending=.pending-messages [role="article"]'] },
	cases: [
		{
			// Neither the turn's name nor its terminal event reaches this page before
			// the timeout. Delayed rows must arrive with restore controls, never rehold.
			name: 'unnamed-stop-times-out-before-the-feed-names-its-turn',
			async setup(ctx) {
				await canWithholdTurnEnd(ctx.page);
				await ctx.page.addInitScript(() => {
					const original = window.fetch;
					window.__feedHeld = new Promise((resolve) => { window.__releaseFeed = resolve; });
					window.fetch = async (input, init) => {
						const url = typeof input === 'string' ? input : input.url;
						if (/\/sessions\/[^/]+\/events/.test(url)) await window.__feedHeld;
						return original(input, init);
					};
				});
				await openStopChat(ctx);
				await ctx.page.waitForTimeout(800);
			},
			async act({ page, mark, variant, log }) {
				await mark('send before the feed connects');
				await typeAndSend(page, `stream tok-${uniq()} ${scriptTag(variant, 'slow', { tps: 2 })}`);
				await waitChat(page, { streaming: 'true' });
				const texts = [`tok-${uniq()}`, `tok-${uniq()}`];
				for (const text of texts) {
					await typeAndSend(page, `queued ${text}`);
					await page.locator('.pending-messages [role="article"]').filter({ hasText: text }).waitFor({ timeout: 5000 });
				}
				await page.waitForFunction((n) => document.querySelectorAll('.pending-messages [data-queue-confirmed="true"]').length === n, texts.length, { timeout: 30_000 });
				await withholdTurnEnd(page, true);
				const collect = await watchStopFrames(page, texts);
				await mark('Stop while the turn is unnamed');
				const pressedAt = Date.now();
				await stopGenerating(page).click();
				await waitChat(page, { streaming: 'false' }, { timeout: STOP_TIMEOUT_MS + 3000 });
				expect(Date.now() - pressedAt >= 8000, 'the unnamed Stop reached timeout recovery');
				await mark('the delayed feed supplies the start and rows, but no terminal event');
				await page.evaluate(() => window.__releaseFeed());
				for (const text of texts) {
					await page.locator('[data-not-sent="true"]').filter({ hasText: text }).getByRole('button', { name: 'Restore to composer' }).waitFor({ timeout: 15_000 });
				}
				await page.waitForTimeout(1000);
				const frames = await collect();
				log(`unnamed timeout: ${frames.length} frames; final ${JSON.stringify(frames.at(-1).messages)}`);
				expectEachOnceInEveryFrame(frames);
				expect((await page.locator(STOPPED_BUBBLE).count()) === 0, 'late turn naming did not put the rows back in bubbles');
				await page.locator('[data-not-sent="true"]').filter({ hasText: texts[0] }).getByRole('button', { name: 'Restore to composer' }).click();
				await page.waitForFunction((text) => document.querySelector('[role="textbox"][aria-label="Message"]')?.textContent?.includes(text), texts[0], { timeout: 5000 });
			}
		},
		{ name: 'timeout-does-not-change-a-later-stop-and-send', ...stopAfterTimeout() },
		{ name: 'timeout-does-not-follow-a-chat-switch-into-stop-and-send', ...stopAfterTimeout({ switchChat: true }) },
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
				// Watch from a confirmed queue, as the other Stop cases do. Before that,
				// a bubble can drop out on its own, with no Stop involved (issue 1052).
				await page.waitForFunction(
					(n) => document.querySelectorAll('.pending-messages [data-queue-confirmed="true"]').length === n,
					texts.length,
					{ timeout: 30_000 }
				);
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
	],
	allow: [...DEV_INSTANCE_NOISE]
});
