// Journey: reload the page while a reply streams. After the reload the same
// chat must come back with its transcript (the user's message once, the reply
// so far), the stream must resume or settle (never a "working" state that does
// not clear), and the reply must end up whole and shown once: no duplicate
// rows, no second copy of the reply text.
//
// Also: the reloaded page goes straight to the chat (no new-chat screen first).
//
// Cases:
//   reload         a plain reload (F5) of the open chat
//   link-reload    the chat opened by a link (/?session=<id>), then reloaded
import { runJourney } from './runner.mjs';
import {
	CHAT,
	SLOW_REPLY,
	chatIdByToken,
	countUserRows,
	expect,
	openNewChat,
	scriptTag,
	typeAndSend,
	uniq,
	waitChat,
	waitTurnSettled
} from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
import { markTime, spans } from './lib/timeline.mjs';

/**
 * How many transcript rows show the slow reply's opening words. Only the
 * innermost rows count: a work log is a row holding the reply's own row.
 */
const replyCopies = (page) =>
	page.evaluate((needle) => {
		const has = (el) => (el.textContent || '').includes(needle);
		return [...document.querySelectorAll('[data-ui-row]')].filter(
			(el) => has(el) && ![...el.querySelectorAll('[data-ui-row]')].some(has)
		).length;
	}, SLOW_REPLY.start);

/**
 * How far the row's Done may lead the chat's own end of stream. Both come from
 * the same `user_streaming_changed` message (CI traces: 3-20ms apart), so this
 * only absorbs a frame or two; a Done during boot or mid-turn is seconds early.
 */
const TURN_END_SKEW_MS = 500;

function reloadCase(name, { viaLink }) {
	return {
		name,
		// Unrecorded: start a slow reply (and, for link-reload, open the chat by
		// its link, which the app consumes and strips from the address).
		async setup(ctx) {
			const { page, variant, status } = ctx;
			await openNewChat(ctx);
			await page.waitForTimeout(800);
			ctx.token = `tok-${uniq()}`;
			await typeAndSend(page, `stream then reload ${ctx.token} ${scriptTag(variant, 'slow', { tps: 4 })}`);
			await page.getByText(SLOW_REPLY.start, { exact: false }).first().waitFor({ timeout: 60_000 });
			await waitChat(page, { streaming: 'true' });
			if (viaLink) {
				const id = await chatIdByToken(page, ctx.token);
				await page.goto(new URL(`/?session=${encodeURIComponent(id)}`, status.urls.ui).toString());
				await waitChat(page, { surface: 'active', loading: 'false', messagesAtLeast: 1 }, { timeout: 60_000 });
			}
			await page.waitForTimeout(500);
		},
		async act(ctx) {
			const { page, mark, log, token } = ctx;
			// Every status the chat's sidebar row shows from the new page's first
			// frame until the reply ends, with the chat's own streaming state in
			// that frame. The turn runs all along, so the row is never Done while
			// the chat still streams or before it has picked the stream up (#1527).
			// The turn's real end may reach the row a few hundred ms before the
			// last words paint (the row follows the global socket, the words the
			// event feed; nothing orders the two), so a Done once the chat itself
			// saw the stream end is the turn ending, not the bug (#1586).
			const id = await chatIdByToken(page, token);
			await page.addInitScript(({ id, end, chat }) => {
				const seen = (window.__rowStatuses = []);
				let chatStreamed = false;
				let last = '';
				let endSeenAt = 0;
				const tick = () => {
					// Watched a moment past the last words, so a Done painted with
					// them is judged with the chat's own end that follows it.
					if (!endSeenAt && (document.body?.textContent || '').includes(end)) endSeenAt = performance.now();
					const done = endSeenAt && performance.now() - endSeenAt > 1000;
					window.__rowWatchDone = !!done;
					const streaming = document.querySelector(chat)?.getAttribute('data-ui-streaming') ?? null;
					if (streaming === 'true') chatStreamed = true;
					const status = document.querySelector(`[data-session-card="${CSS.escape(id)}"]`)?.getAttribute('data-status');
					const key = `${status}|${streaming}`;
					if (status && key !== last) {
						last = key;
						seen.push({ status, streaming, chatStreamed, at: Math.round(performance.now()) });
					}
					if (!done) requestAnimationFrame(tick);
				};
				requestAnimationFrame(tick);
			}, { id, end: SLOW_REPLY.end, chat: CHAT });
			await mark('reload');
			const t0 = Date.now();
			await page.reload({ waitUntil: 'domcontentloaded' });
			// A new step once the recorder has seen the old page go (two painted
			// frames of the new document): the reload itself (chat → nothing) is not
			// the app going backwards; what boots next is judged.
			await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => done()))));
			await mark('page reloaded');
			await waitChat(page, { surface: 'active', loading: 'false', session: /^s:/, messagesAtLeast: 1 }, { timeout: 60_000 });
			log(`transcript back ${Date.now() - t0}ms after reload`);
			expect((await countUserRows(page, token)) === 1, 'the message is back, once');

			await page.getByText(SLOW_REPLY.end, { exact: false }).first().waitFor({ timeout: 90_000 });
			await page.waitForFunction(() => window.__rowWatchDone === true, null, { timeout: 10_000 });
			const statuses = await page.evaluate(() => window.__rowStatuses);
			const shown = statuses.map((s) => `${s.status} (chat streaming=${s.streaming}${s.chatStreamed ? '' : ', not yet streamed'}) @${s.at}`).join(' → ');
			log(`sidebar row while the turn ran: ${shown}`);
			const firstDone = statuses.findIndex((s) => s.status === 'done');
			if (firstDone >= 0) {
				// Done only for the turn's real end: the chat had picked the stream
				// up, sees it stop at about the same moment (both follow the same
				// socket message; the row's store can paint a frame first), and
				// nothing after says the turn still runs.
				const early = statuses[firstDone];
				const after = statuses.slice(firstDone);
				const chatEnd = after.find((s) => s.streaming === 'false');
				expect(early.chatStreamed && chatEnd && chatEnd.at - early.at <= TURN_END_SKEW_MS, `the running chat's row said Done after the reload while the chat still streamed: ${shown}`);
				expect(after.every((s) => s.status === 'done'), `the row went back to working after Done: ${shown}`);
				expect(!after.slice(after.indexOf(chatEnd)).some((s) => s.streaming === 'true'), `the chat streamed again after its row said Done: ${shown}`);
			}
			await waitTurnSettled(page, { messagesAtLeast: 2, timeout: 30_000 });
			log(`reply settled ${Date.now() - t0}ms after reload`);
			expect((await countUserRows(page, token)) === 1, 'the message shows once after the reply settled');
			const copies = await replyCopies(page);
			expect(copies === 1, `the reply shows once (${copies} copies)`);
		},
		verify({ recording }) {
			// The reloaded page goes straight to this chat: never the new-chat
			// screen or the centred composer on the way.
			const from = markTime(recording, 'page reloaded');
			const detour = spans(recording, (s) => s.surface === 'new' || s.composer === 'centre', { from });
			expect(detour.length === 0, `the reload showed the new-chat screen first: ${JSON.stringify(detour)}`);
		}
	};
}

await runJourney({
	name: 'reload-midstream',
	cases: [reloadCase('reload', { viaLink: false }), reloadCase('link-reload', { viaLink: true })],
	allow: [...DEV_INSTANCE_NOISE]
});
