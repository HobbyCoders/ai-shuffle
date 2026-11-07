// Journey: the red error banner a failed turn raises belongs to that turn. It
// goes when the turn is no longer the chat's latest outcome, and only then.
//
// Cases:
//   rewind-failed-turn  (#1082) a turn fails with HTTP 500; rewinding its message
//                       removes the turn, so its banner goes with it and the
//                       words come back to the composer.
//   queued-follow-up    (#1175) a message queued while a turn runs is answered
//                       after that turn fails; the successful reply clears the
//                       failed turn's banner, so the latest send does not look
//                       failed.
import { fakeAiTag, runJourney } from './runner.mjs';
import { composer, countUserRows, expect, openNewChat, scriptTag, typeAndSend, uniq, waitChat, waitTurnSettled } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const HTTP_500 = /HTTP 500/;
/** The chat's own error banner (the conversation log's alert). */
const banner = (page) => page.getByRole('log', { name: 'Conversation messages' }).getByRole('alert');

async function sendAndSettle(page, variant, words, messagesAtLeast) {
	await typeAndSend(page, `${words} ${fakeAiTag(variant)}`.trim());
	await waitTurnSettled(page, { messagesAtLeast, text: `You said: “${words}` });
}

/** Send a turn that fails with HTTP 500 and wait until it has ended with its banner up. */
async function sendFailingTurn(page, variant, words) {
	await typeAndSend(page, `${words} ${fakeAiTag(variant, 'error-500')}`);
	await banner(page).filter({ hasText: HTTP_500 }).first().waitFor({ timeout: 120_000 });
	await waitChat(page, { surface: 'active', streaming: 'false' }, { timeout: 30_000 });
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
				await banner(page).first().waitFor({ state: 'detached', timeout: 5000 }).catch(() => {});
				await page.waitForTimeout(1500);
				expect((await banner(page).count()) === 0, 'the failed turn\'s banner left with the turn it belonged to');
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
				const left = await banner(page).allInnerTexts();
				expect(left.length === 0, `the failed turn's banner is gone once the follow-up succeeded, not ${JSON.stringify(left)}`);
			}
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
});
