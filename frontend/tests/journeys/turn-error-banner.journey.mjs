// Journey: a failed turn's error belongs to that turn. Its transcript Error row
// is the durable record; the red banner above the composer steps aside while
// that row says the same words (#1341), and whatever shows goes when the turn
// is no longer the chat's latest outcome, and only then.
//
// Cases:
//   failed-turn-shows-its-error-once  (#1341) a turn fails with HTTP 500. On
//                       every painted frame from Send until 2 s after the turn
//                       settled, at most one visible element says the error
//                       (banner or Error row, never both), and exactly one at
//                       the end.
//   rewind-failed-turn  (#1082) a turn fails with HTTP 500; rewinding its message
//                       removes the turn, so its Error row (and any banner) goes
//                       with it and the words come back to the composer.
//   queued-follow-up    (#1175) a message queued while a turn runs is answered
//                       after that turn fails; no banner is left, so the latest
//                       send does not look failed. The failed turn's Error row
//                       stays above the reply as its record.
import { fakeAiTag, runJourney } from './runner.mjs';
import {
	composer, countUserRows, errorBanner, errorRow, expect, openNewChat, scriptTag, typeAndSend, uniq,
	waitChat, waitTurnSettled, watchVisibleCopies
} from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const HTTP_500 = /HTTP 500/;

async function sendAndSettle(page, variant, words, messagesAtLeast) {
	await typeAndSend(page, `${words} ${fakeAiTag(variant)}`.trim());
	await waitTurnSettled(page, { messagesAtLeast, text: `You said: “${words}` });
}

/** Wait until a failed turn has ended with its Error row in the transcript. */
async function waitTurnFailed(page) {
	await errorRow(page, HTTP_500).first().waitFor({ timeout: 120_000 });
	await waitChat(page, { surface: 'active', streaming: 'false' }, { timeout: 30_000 });
}

/** Send a turn that fails with HTTP 500 and wait until it has ended. */
async function sendFailingTurn(page, variant, words) {
	await typeAndSend(page, `${words} ${fakeAiTag(variant, 'error-500')}`);
	await waitTurnFailed(page);
}

/** Rewind the last user message from its own row and wait for the server's answer. */
async function rewindLastUserMessage(page) {
	await page.getByTitle('Rewind to before this message', { exact: true }).last().focus();
	await page.keyboard.press('Enter');
	const answered = page.waitForResponse((r) => r.url().includes('/rewind/execute/') && r.request().method() === 'POST');
	await page.getByRole('dialog').getByRole('button', { name: 'Rewind', exact: true }).click();
	const result = await (await answered).json();
	expect(result.success && result.chat_rewound, `rewind succeeded: ${JSON.stringify(result)}`);
	await page.getByRole('dialog').waitFor({ state: 'hidden' });
}

await runJourney({
	name: 'turn-error-banner',
	cases: [
		{
			name: 'failed-turn-shows-its-error-once',
			async setup(ctx) {
				await openNewChat(ctx);
				await ctx.page.waitForTimeout(800);
			},
			async act({ page, mark, variant, log }) {
				const stop = await watchVisibleCopies(page, 'HTTP 500');
				await mark('send a turn that will fail');
				await typeAndSend(page, `once-${uniq()} ${fakeAiTag(variant, 'error-500')}`);
				await waitTurnFailed(page);
				await page.waitForTimeout(2000);
				const copies = await stop();
				// The runner prints this under the case's PASS/FAIL line.
				log(`error copies: per-frame max ${copies.max}, final ${copies.last}, over ${copies.frames} frames`);
				expect(copies.frames > 10, `the error was watched over painted frames (${copies.frames})`);
				expect(copies.max < 2, `the error never shows twice in one frame (max ${copies.max} visible copies)`);
				expect(copies.last === 1, `the error shows exactly once once the turn settled (${copies.last} visible copies)`);
			}
		},
		{
			name: 'rewind-failed-turn',
			async setup(ctx) {
				await openNewChat(ctx);
				const kept = `kept-${uniq()}`;
				await sendAndSettle(ctx.page, ctx.variant, kept, 2);
				const failed = `failed-${uniq()}`;
				await sendFailingTurn(ctx.page, ctx.variant, failed);
				ctx.state = { kept, failed };
			},
			async act({ page, mark, state }) {
				await mark('rewind the failed turn');
				await rewindLastUserMessage(page);
				await waitChat(page, { messages: '2', loading: 'false', streaming: 'false' }, { timeout: 15_000 });
				await errorRow(page, HTTP_500).first().waitFor({ state: 'detached', timeout: 5000 }).catch(() => {});
				await page.waitForTimeout(1500);
				expect((await errorBanner(page).count()) === 0, 'no banner is left for the rewound turn');
				expect((await errorRow(page, HTTP_500).count()) === 0, 'the failed turn\'s Error row left with the turn it belonged to');
				expect((await composer(page).innerText()).includes(state.failed), 'the failed words came back to the composer');
				expect((await countUserRows(page, state.kept)) === 1, 'the earlier turn stayed');
			}
		},
		{
			name: 'queued-follow-up',
			async setup(ctx) {
				await openNewChat(ctx);
				await ctx.page.waitForTimeout(800);
			},
			async act({ page, mark, variant }) {
				await mark('send a turn that will fail');
				const failed = `failed-${uniq()}`;
				await typeAndSend(page, `${failed} ${scriptTag(variant, 'error-500', { latency_ms: 1500 })}`);
				await waitChat(page, { streaming: 'true' }, { timeout: 30_000 });
				await mark('queue a follow-up');
				const follow = `follow-${uniq()}`;
				await typeAndSend(page, `${follow} ${fakeAiTag(variant)}`.trim());
				await waitTurnSettled(page, { text: `You said: “${follow}`, messagesAtLeast: 3, timeout: 120_000 });
				await page.waitForTimeout(1500);
				expect((await page.locator('.pending-messages [role="article"]').count()) === 0, 'no pending bubble left behind');
				expect((await countUserRows(page, follow)) === 1, 'the follow-up was delivered once');
				const left = await errorBanner(page).allInnerTexts();
				expect(left.length === 0, `no banner is left once the follow-up succeeded, not ${JSON.stringify(left)}`);
				// The failed turn's Error row is its durable record, above the reply.
				const row = errorRow(page, HTTP_500);
				expect((await row.count()) === 1, 'the failed turn keeps its one Error row');
				const reply = page.getByRole('log', { name: 'Conversation messages' }).getByText(`You said: “${follow}`).first();
				const [rowBox, replyBox] = [await row.first().boundingBox(), await reply.boundingBox()];
				expect(rowBox && replyBox && rowBox.y < replyBox.y, `the Error row stays above the successful reply (${rowBox?.y} vs ${replyBox?.y})`);
			}
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
});
