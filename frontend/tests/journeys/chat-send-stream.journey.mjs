// Journey: send a message in a new chat and watch the reply stream and settle.
//
// Guards: the reply streams in and settles (streaming → false, the reply's
// text on screen), every row appears once (the user's message text exactly
// once, no duplicate row ids), the surface only moves forward, nothing
// remounts, and the Stop button follows the turn on every painted frame:
// shown while it streams, gone once it ends (a stuck Stop is the "still
// working" bug owners find by hand).
//
// Cases:
//   short       the default echo reply, first message of a new chat
//   long        [script:long]: paragraphs, a code block, a table, a list
//   send-on-load  a returning visit (layout saved New Chat): type and send the
//               moment the composer shows, while a boot load is still held; the
//               text survives to Send and the created chat stays open (#811)
//   follow-up   a second message in the same chat once the first settled
import { fakeAiTag, runJourney } from './runner.mjs';
import {
	composer,
	countText,
	countUserRows,
	expect,
	holdBootLoad,
	openNewChat,
	sendButton,
	settleServiceWorker,
	typeAndSend,
	uniq,
	waitChat,
	waitTurnSettled
} from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
import { STOP_SELECTOR, expectStopFollowsStreaming, markTime } from './lib/timeline.mjs';

const LONG_END = 'That is the end of the long reply.';
/** One of the boot's first loads (AppPage), held so a send lands before the boot reconciles. */
const BOOT_HOLD = { path: '/api/v1/tags', ms: 6000 };

/** Send `text` and check the settled chat: reply shown, user text once, composer empty and usable. */
async function sendAndSettle({ page, mark, log }, { text, replyText, messagesAtLeast }) {
	await mark('send');
	const t0 = Date.now();
	await typeAndSend(page, text);
	await waitTurnSettled(page, { text: replyText, messagesAtLeast });
	log(`reply settled ${Date.now() - t0}ms after send`);
	const token = text.split(' ').find((word) => word.startsWith('tok-'));
	expect((await countUserRows(page, token)) === 1, `the user's message (${token}) shows exactly once`);
	expect((await composer(page).innerText()).trim() === '', 'the composer is empty after the send');
	const selection = await page.evaluate(() => ({
		session: JSON.parse(sessionStorage.getItem('ai-shuffle-active-session') || 'null')?.sessionId,
		view: JSON.parse(localStorage.getItem('ai-shuffle-layout') || 'null')?.activeView
	}));
	expect(!!selection.session && selection.session === selection.view?.sessionId, 'the layout follows the sent chat, including a first send during boot');
}

await runJourney({
	name: 'chat-send-stream',
	recorder: { selectors: [STOP_SELECTOR] },
	cases: [
		{
			name: 'short',
			async setup(ctx) {
				await openNewChat(ctx);
				await ctx.page.waitForTimeout(800);
			},
			async act(ctx) {
				await sendAndSettle(ctx, {
					text: `hello tok-${uniq()} ${fakeAiTag(ctx.variant)}`.trim(),
					replyText: 'Fake AI here',
					messagesAtLeast: 2
				});
			},
			verify: ({ recording }) => expectStopFollowsStreaming(recording, { from: markTime(recording, 'send') })
		},
		{
			name: 'long',
			async setup(ctx) {
				await openNewChat(ctx);
				await ctx.page.waitForTimeout(800);
			},
			async act(ctx) {
				const { page } = ctx;
				await sendAndSettle(ctx, {
					text: `write a long answer tok-${uniq()} ${fakeAiTag(ctx.variant, 'long')}`,
					replyText: LONG_END,
					messagesAtLeast: 2
				});
				// The code block and the table rendered as such, not as raw markdown.
				await page.locator('pre').filter({ hasText: 'def fibonacci' }).first().waitFor({ timeout: 5000 });
				await page.locator('table').filter({ hasText: 'slow tokens' }).first().waitFor({ timeout: 5000 });
				expect((await countText(page, '```')) === 0, 'no raw markdown fences on screen');
			},
			verify: ({ recording }) => expectStopFollowsStreaming(recording, { from: markTime(recording, 'send') })
		},
		{
			// #811: a returning visit whose first message is typed and sent the
			// moment the composer shows, while the boot's first loads still run
			// (one held, as a slow network would). The send created its chat;
			// when the loads ended, the boot took that chat for a stale restored
			// one (the layout had saved New Chat) and closed it: the chat was in
			// the sidebar, the page back on an empty New Chat.
			name: 'send-on-load',
			async setup(ctx) {
				const { page, status } = ctx;
				// The first visit saves the layout's New Chat view, as any earlier
				// visit does; a reload comes back to it.
				await openNewChat(ctx);
				await settleServiceWorker(page, status);
				await holdBootLoad(page, BOOT_HOLD.path, BOOT_HOLD.ms);
				// The returning visit: act starts on this load while its held boot
				// load is still out (a reload inside a recorded step reads as the
				// surface going away and coming back).
				await page.reload({ waitUntil: 'domcontentloaded' });
				await composer(page).waitFor({ timeout: 60_000 });
			},
			async act(ctx) {
				const { page, mark, log, variant } = ctx;
				const token = `tok-${uniq()}`;
				const text = `on load ${token} ${fakeAiTag(variant)}`.trim();
				await mark('type and send at once');
				const box = composer(page);
				await box.click();
				await box.pressSequentially(text);
				// What the user typed is still there when they press Send.
				const typed = (await box.innerText()).trim();
				expect(typed === text, `the composer still holds what was typed (${JSON.stringify(typed)})`);
				await sendButton(page).click();
				const hold = await page.evaluate(() => window.__journeyBootHold);
				expect(hold?.startedAt !== null && hold?.releasedAt === null, `Send was pressed while the held boot load (${BOOT_HOLD.path}) was still out: ${JSON.stringify(hold)}`);
				await waitTurnSettled(page, { text: 'Fake AI here', messagesAtLeast: 2, hold: 0 });
				// The boot reconciles the chat with the layout once its loads end.
				await page.waitForFunction(() => window.__journeyBootHold?.releasedAt != null, null, { timeout: BOOT_HOLD.ms + 30_000 });
				log(`boot load held ${BOOT_HOLD.ms}ms; reply settled before it was let go`);
				await mark('the boot loads end');
				await page.waitForTimeout(2000);
				await waitChat(page, { surface: 'active', session: /^s:/, streaming: 'false', messagesAtLeast: 2 }, { timeout: 5000 });
				expect((await countUserRows(page, token)) === 1, `the first message (${token}) shows exactly once`);
				// The layout follows the created chat, so a reload reopens it.
				const view = await page.evaluate(() => JSON.parse(localStorage.getItem('ai-shuffle-layout') || 'null')?.activeView);
				expect(view?.type === 'session' && typeof view.sessionId === 'string', `the layout names the created chat (${JSON.stringify(view)})`);
			},
			verify: ({ recording }) => expectStopFollowsStreaming(recording, { from: markTime(recording, 'type and send at once') })
		},
		{
			name: 'follow-up',
			async setup(ctx) {
				const { page } = ctx;
				await openNewChat(ctx);
				await typeAndSend(page, `first tok-${uniq()}`);
				await waitTurnSettled(page, { text: 'Fake AI here', messagesAtLeast: 2 });
			},
			async act(ctx) {
				await sendAndSettle(ctx, {
					text: `second tok-${uniq()} ${fakeAiTag(ctx.variant)}`.trim(),
					replyText: 'You said: “second',
					messagesAtLeast: 4
				});
			},
			verify: ({ recording }) => expectStopFollowsStreaming(recording, { from: markTime(recording, 'send') })
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
});
