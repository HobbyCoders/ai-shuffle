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
//   follow-up   a second message in the same chat once the first settled
import { fakeAiTag, runJourney } from './runner.mjs';
import { composer, countText, countUserRows, expect, openNewChat, typeAndSend, uniq, waitTurnSettled } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
import { STOP_SELECTOR, expectStopFollowsStreaming, markTime } from './lib/timeline.mjs';

const LONG_END = 'That is the end of the long reply.';

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
