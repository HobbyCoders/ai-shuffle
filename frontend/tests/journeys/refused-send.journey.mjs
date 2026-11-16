// Journey: a send the server refuses, in the two orderings #1556 fixed that
// depend on timing (#1577). The server's refusal is the chat socket's `error`
// frame naming the send's `client_message_id`; the journey drives the order
// of delivery with two hooks, not a stubbed store:
//
//   - the chat socket goes through page.routeWebSocket, so a `query` frame can
//     be rewritten or answered with the refusal, and frames said to the page;
//   - the session event feed (an SSE body read through fetch) can be gated in
//     the page, so its frames arrive after the socket's.
//
// Cases (desktop and phone, fast and slow):
//   worktree-fallback-refused  an auto-worktree first send whose chat is created
//               without its turn (`query_started: false`: the create request is
//               sent without the prompt), so the turn goes out over the socket
//               (the fallback), and the server refuses it. Stop clears, nothing
//               streams after, the row goes and the composer gets the words
//               back (the last two fail today: known bug #1677).
//   start-ahead-of-feed-refused  an idle send in a chat with history: the socket
//               announces another turn (`start`) before the feed shows it, then
//               the original send is refused. The refused row goes, Stop stays
//               while the feed is still behind, and the announced turn streams
//               and settles once the feed catches up (#1258). The feed catches
//               up in two gated steps (all but the turn's end, then the end), so
//               the streaming frame never depends on the slow reply's length.
import { randomBytes } from 'node:crypto';
import { fakeAiTag, runJourney } from './runner.mjs';
import { PROJECT, SLOW_REPLY, chatState, composer, countUserRows, expect, openNewChat, pickAccess, scriptTag, stopButton, typeAndSend, uniq, waitTurnSettled, withChatSetup } from './lib/app.mjs';
import { STOP_SELECTOR, anyFrame, markTime, spans } from './lib/timeline.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const REFUSAL = 'refused by the journey';
const FALLBACK_REFUSED_STEP = 'the server refuses the fallback send';
const REFUSED_STEP = 'the socket announces another turn ahead of the feed, then refuses this send';
const CAUGHT_UP_STEP = "the feed catches up to the announced turn, its end still held: the turn streams";
const ENDED_STEP = "the announced turn's end arrives: the turn settles";

/** A correlation id in the server's accepted form (`client-` and 32 hex digits). */
const clientMessageId = () => `client-${randomBytes(16).toString('hex')}`;

/**
 * The chat socket through a hook. `onQuery(frame, ws, server)` decides what
 * happens to each client `query` frame (return true when it handled it).
 */
async function hookChatSocket(page) {
	const hook = { onQuery: null, queries: [] };
	await page.routeWebSocket(/\/api\/v1\/ws\/chat/, (ws) => {
		const server = ws.connectToServer();
		ws.onMessage((message) => {
			let frame = null;
			try { frame = JSON.parse(String(message)); } catch { /* not JSON */ }
			if (frame?.type === 'query') {
				hook.queries.push(frame);
				if (hook.onQuery?.(frame, ws, server)) return;
			}
			server.send(message);
		});
		server.onMessage((message) => ws.send(message));
	});
	return hook;
}

/** The server refusing `frame`, as the chat socket says it. */
function refusal(frame) {
	return JSON.stringify({ type: 'error', session_id: frame.session_id, client_message_id: frame.client_message_id, message: REFUSAL });
}

/**
 * Gate the event feed in the page, one SSE event at a time, in order:
 *   - `mode = 'hold'`: every event waits;
 *   - `releaseUntilEnd()`: events go through up to a turn's end
 *     (`turn.completed`, `turn.aborted`, `turn.parked`), which waits, and so
 *     does everything behind it;
 *   - `release()`: everything goes through.
 * Splitting by event, not by network chunk, matters: a turn that finished on
 * the server while the feed was held may reach the page in one chunk, deltas
 * and end together, and the end must still be held apart from the deltas.
 */
async function gateFeed(page) {
	await page.addInitScript(() => {
		const END = /^event: turn\.(completed|aborted|parked)\s*$/m;
		const gate = { mode: 'open', waiting: [], held: 0, endsHeld: 0 };
		const wake = () => { for (const go of gate.waiting.splice(0)) go(); };
		gate.releaseUntilEnd = () => { gate.mode = 'until-end'; wake(); };
		gate.release = () => { gate.mode = 'open'; wake(); };
		const blocked = (event) => gate.mode === 'hold' || (gate.mode === 'until-end' && END.test(event));
		window.__journeyFeed = gate;
		const original = window.fetch.bind(window);
		window.fetch = async (input, init) => {
			const response = await original(input, init);
			const url = typeof input === 'string' ? input : input?.url || '';
			if (!/\/sessions\/[^/]+\/events\?/.test(url) || !response.body) return response;
			const decoder = new TextDecoder();
			const encoder = new TextEncoder();
			let buffer = '';
			const body = response.body.pipeThrough(new TransformStream({
				async transform(chunk, controller) {
					buffer += decoder.decode(chunk, { stream: true });
					let cut;
					while ((cut = buffer.indexOf('\n\n')) >= 0) {
						const event = buffer.slice(0, cut + 2);
						buffer = buffer.slice(cut + 2);
						if (blocked(event)) {
							gate.held += 1;
							let countedEnd = false;
							while (blocked(event)) {
								if (!countedEnd && gate.mode === 'until-end') { gate.endsHeld += 1; countedEnd = true; }
								await new Promise((go) => gate.waiting.push(go));
							}
						}
						controller.enqueue(encoder.encode(event));
					}
				},
				flush(controller) {
					buffer += decoder.decode();
					if (buffer) controller.enqueue(encoder.encode(buffer));
				}
			}));
			return new Response(body, { status: response.status, statusText: response.statusText, headers: response.headers });
		};
	});
}

/** Poll `test` (a Node-side check) until it holds or `ms` pass. */
async function until(page, test, ms = 15_000) {
	const deadline = Date.now() + ms;
	while (!test() && Date.now() < deadline) await page.waitForTimeout(50);
	return test();
}

const stopShown = async (page) => (await stopButton(page).count()) > 0 && (await stopButton(page).first().isVisible());

/** The composer's words, trimmed. */
const composerText = async (page) => (await composer(page).innerText()).trim();

const worktreeFallbackRefused = {
	name: 'worktree-fallback-refused',
	recorder: { selectors: [STOP_SELECTOR] },
	knownBug: {
		issue: 1677,
		error: /the refused row is gone/,
		reason: 'the fallback send mints its own client_message_id, so the refusal finds no row to retract or restore'
	},
	async setup(ctx) {
		const { page } = ctx;
		ctx.hook = await hookChatSocket(page);
		// The chat is created, but without its turn: no prompt rides the create
		// request, so the backend answers `query_started: false`.
		// In the page, not page.route: the built app's service worker makes the
		// requests page.route would see (lib/app.mjs).
		await page.addInitScript(() => {
			const seen = { prompted: null, commandId: null, created: [] };
			window.__journeyWorktree = seen;
			const original = window.fetch.bind(window);
			window.fetch = async (input, init) => {
				const url = typeof input === 'string' ? input : input?.url || '';
				if (!/\/sessions\/with-worktree(\?|$)/.test(url) || (init?.method || 'GET').toUpperCase() !== 'POST' || typeof init?.body !== 'string') {
					return original(input, init);
				}
				const body = JSON.parse(init.body);
				seen.prompted = typeof body.prompt === 'string' && body.prompt.length > 0;
				seen.commandId = body.client_message_id || null;
				delete body.prompt;
				const response = await original(input, { ...init, body: JSON.stringify(body) });
				try { seen.created.push(await response.clone().json()); } catch { seen.created.push(null); }
				return response;
			};
		});
		const hero = await openNewChat(ctx, { project: PROJECT });
		await pickAccess(page, 'Bypass');
		await withChatSetup(page, async () => {
			await hero.getByRole('button', { name: /Switch to a worktree$/ }).or(hero.locator('.wt-chip')).click();
			await hero.getByRole('button', { name: /^Worktree branch:/ }).waitFor();
		});
		await withChatSetup(page, () => hero.getByRole('button', { name: /^Worktree branch: (a new branch from|Auto from)/ }).waitFor());
	},
	async act(ctx) {
		const { page, mark, variant, hook, log } = ctx;
		const words = `refused fallback ${uniq()} ${fakeAiTag(variant)}`.trim();
		// The fallback's turn goes out over the socket: the server refuses it.
		hook.onQuery = (frame, ws) => {
			if (!frame.client_message_id) return false;
			ctx.refusedQuery = frame;
			ctx.deliver = () => ws.send(refusal(frame));
			return true;
		};
		await mark('send the first message: the chat is made without its turn, which goes out over the socket');
		await typeAndSend(page, words);
		expect(await until(page, () => !!ctx.deliver), 'the first turn went out over the socket');
		await mark(FALLBACK_REFUSED_STEP);
		ctx.deliver();
		await page.getByText('Message not sent', { exact: false }).first().waitFor({ timeout: 30_000 });
		await page.waitForTimeout(2500); // a late fold or feed restart would raise Stop again now
		Object.assign(ctx, await page.evaluate(() => ({ ...window.__journeyWorktree })));
		expect(ctx.prompted === true, 'the auto-worktree create request carried the prompt (the started path)');
		expect(ctx.created.length === 1 && ctx.created[0].query_started === false, `the chat was created without its turn: ${JSON.stringify(ctx.created.map((c) => c.query_started))}`);
		expect(!!ctx.refusedQuery && ctx.refusedQuery.session_id === ctx.created[0].session_id, 'the first turn went out over the socket for the new chat (the fallback)');
		expect(!(await stopShown(page)), 'Stop is gone after the refusal');
		const state = await chatState(page);
		expect(state?.streaming === 'false', `nothing streams after the refusal: ${JSON.stringify(state)}`);
		log(`refused ${ctx.refusedQuery.client_message_id} on ${ctx.created[0].session_id}; row made with ${ctx.commandId}`);
		expect((await countUserRows(page, words)) === 0, 'the refused row is gone');
		expect((await composerText(page)) === words, `the composer has the words back: "${await composerText(page)}"`);
	},
	verify({ recording }) {
		const refusedAt = markTime(recording, FALLBACK_REFUSED_STEP);
		const tail = spans(recording, (s) => Number(s.stop) > 0, { from: refusedAt });
		const last = tail[tail.length - 1];
		expect(!last || last.end < recording.durationMs - 2000, `Stop is still shown at the end of the case: ${JSON.stringify(tail)}`);
	}
};

const startAheadOfFeedRefused = {
	name: 'start-ahead-of-feed-refused',
	recorder: { selectors: [STOP_SELECTOR] },
	async setup(ctx) {
		const { page } = ctx;
		ctx.hook = await hookChatSocket(page);
		ctx.seen = {}; // act's ctx is a copy: what verify reads lives here
		await gateFeed(page);
		await openNewChat(ctx, { project: PROJECT });
		await pickAccess(page, 'Bypass');
		await typeAndSend(page, `first ${uniq()} ${fakeAiTag(ctx.variant)}`.trim());
		await waitTurnSettled(page, { messagesAtLeast: 2 });
	},
	async act(ctx) {
		const { page, mark, variant, hook, log } = ctx;
		const words = `refused idle ${uniq()}`;
		const announced = `announced ${uniq()} ${scriptTag(variant, 'slow', { tps: 6 })}`;
		const before = Number((await chatState(page))?.messages || 0);
		// The socket carries another turn instead of this send: it runs on the
		// server, whose events reach the page only through the held feed. Then
		// the socket announces it (`start`, as an accepted turn does) and
		// refuses this send. Today's server announces turns through the feed
		// alone, so the journey says the `start` itself.
		hook.onQuery = (frame, ws, server) => {
			if (!frame.client_message_id || ctx.swapped) return false;
			ctx.swapped = frame;
			server.send(JSON.stringify({ ...frame, prompt: announced, client_message_id: clientMessageId() }));
			ctx.deliver = () => {
				ws.send(JSON.stringify({ type: 'start', session_id: frame.session_id }));
				ws.send(refusal(frame));
				ctx.refusedAt = Date.now();
			};
			return true;
		};
		await page.evaluate(() => { window.__journeyFeed.mode = 'hold'; });
		await mark('send while the feed is held');
		await typeAndSend(page, words);
		expect(await until(page, () => !!ctx.deliver), 'the send went out over the socket');
		expect((await countUserRows(page, words)) === 1, 'the in-flight send has its row');
		// Send and the refusal are separate steps: the row going is the refusal's
		// doing, not a reversal.
		await mark(REFUSED_STEP);
		ctx.deliver();
		await page.getByText('Message not sent', { exact: false }).first().waitFor({ timeout: 30_000 });
		// The announced turn's first feed frames reach the gate and wait there.
		await page.waitForFunction(() => window.__journeyFeed.held > 0, null, { timeout: 30_000 }).catch(() => {});
		await page.waitForTimeout(1000);
		expect(!!ctx.refusedAt, "the refusal followed the socket's start");
		expect((await countUserRows(page, words)) === 0, 'the refused row is gone');
		expect(await stopShown(page), 'Stop stays for the announced turn while the feed is behind');
		const held = await page.evaluate(() => window.__journeyFeed.held);
		expect(held > 0, `the feed had frames held behind the socket (${held})`);
		// The feed catches up in two steps, so the live reply is held by the gate,
		// not by the slow reply outlasting the steps above: everything up to the
		// turn's end goes through first, and the announced turn's reply must show
		// while it is still live, however long ago the server finished it; then
		// the end goes through. (`streaming` alone proves nothing here: the send's
		// optimistic flip has held it true since Send.)
		ctx.seen.before = before;
		await mark(CAUGHT_UP_STEP);
		await page.evaluate(() => window.__journeyFeed.releaseUntilEnd());
		await page.getByText(SLOW_REPLY.start, { exact: false }).first().waitFor({ timeout: 30_000 });
		let live = null;
		for (const deadline = Date.now() + 15_000; Date.now() < deadline;) {
			live = await chatState(page);
			if (Number(live?.messages || 0) >= before + 2) break;
			await page.waitForTimeout(100);
		}
		expect(Number(live?.messages || 0) >= before + 2 && live?.streaming === 'true', `the announced turn's reply shows while the turn is live: ${JSON.stringify(live)}`);
		expect(await stopShown(page), 'Stop shows while the announced turn streams');
		const endsHeld = await page.evaluate(() => window.__journeyFeed.endsHeld);
		await mark(ENDED_STEP);
		await page.evaluate(() => window.__journeyFeed.release());
		await waitTurnSettled(page, { messagesAtLeast: before + 2, text: SLOW_REPLY.end });
		expect(!(await stopShown(page)), 'Stop is gone once the announced turn settled');
		expect((await countUserRows(page, words)) === 0, 'the refused row does not come back');
		expect((await composerText(page)) === words, `the composer has the refused words back: "${await composerText(page)}"`);
		log(`held ${held} feed event(s); turn end held ${endsHeld} time(s) while the reply showed; refused ${ctx.swapped.client_message_id}`);
	},
	verify({ recording, seen: { before } }) {
		const refused = markTime(recording, REFUSED_STEP);
		const caughtUp = markTime(recording, CAUGHT_UP_STEP);
		// From the refusal until the feed shows the turn, Stop never leaves.
		expect(anyFrame(recording, (s) => Number(s.stop) > 0, { from: markTime(recording, 'send while the feed is held') }), 'Stop showed after Send');
		const gaps = spans(recording, (s) => !(Number(s.stop) > 0), { from: refused }).filter((g) => g.start < caughtUp);
		expect(gaps.length === 0, `Stop left before the feed caught up: ${JSON.stringify(gaps)}`);
		// The turn's end was still held, so this frame cannot be skipped past:
		// the announced reply on screen while the turn is live.
		const ended = markTime(recording, ENDED_STEP);
		const liveReply = (s) => s.streaming === 'true' && Number(s.messages) >= before + 2;
		expect(spans(recording, liveReply, { from: caughtUp }).some((s) => s.start < ended), 'the announced turn streamed once the feed caught up, before its end arrived');
	}
};

await runJourney({
	name: 'refused-send',
	// The fetch wrappers see the page's own requests only while the product
	// service worker is kept out (CI's loopback instance).
	browser: async () => ({ context: { serviceWorkers: 'block' } }),
	cases: [worktreeFallbackRefused, startAheadOfFeedRefused],
	allow: [...DEV_INSTANCE_NOISE]
});
