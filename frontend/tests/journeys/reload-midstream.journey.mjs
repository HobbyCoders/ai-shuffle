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
