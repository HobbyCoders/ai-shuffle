// Journey: a cleared chat. Clearing wipes a chat's conversation in place: the
// chat keeps its id, title and project. It must read as that chat, cleared
// (its own title and "Conversation cleared"), never as the new-session hero,
// and a send in it must work and start fresh (no old rows coming back).
//
// Cases:
//   seeded             open the seed's cleared chat from the new-session screen
//                      (read only: the seed's chat must stay cleared for later runs)
//   clear-then-send    a chat this run made: the server clears it while it is
//                      open (POST /sessions/<id>/clear, the clear the app's
//                      `user_session_cleared` broadcast reports), then a send
import { fakeAiTag, runJourney } from './runner.mjs';
import {
	SEEDED,
	api,
	chatIdByToken,
	countUserRows,
	expect,
	gotoApp,
	openFromSidebar,
	openNewChat,
	typeAndSend,
	uniq,
	waitChat,
	waitTurnSettled
} from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
import { expectComposerPositions } from './lib/timeline.mjs';

/** The cleared state: its own title and hint, composer centred, no new-session hero. */
async function expectClearedState(page, title) {
	await waitChat(page, { surface: 'cleared', composer: 'centre', loading: 'false', session: /^s:/ });
	await page.getByText('Conversation cleared', { exact: false }).waitFor();
	if (title) await page.getByRole('heading', { name: title, exact: true }).waitFor();
	expect(!(await page.getByRole('region', { name: 'New session' }).isVisible()), 'the new-session hero is not shown');
	expect((await page.locator('[data-ui-mount="NewSessionHero"]').count()) === 0, 'NewSessionHero is not mounted');
}

await runJourney({
	name: 'cleared-chat',
	cases: [
		{
			name: 'seeded',
			async setup({ page, status }) {
				await gotoApp(page, status);
				await waitChat(page, { surface: 'new' });
				await page.waitForTimeout(1000);
			},
			async act({ page, mark }) {
				await mark('open the cleared chat');
				await openFromSidebar(page, SEEDED.cleared);
				await expectClearedState(page, SEEDED.cleared);
				await page.waitForTimeout(1500);
				await expectClearedState(page, SEEDED.cleared);
			},
			// Opened from the new-session screen, the composer never moves.
			verify: ({ recording }) => expectComposerPositions(recording, ['centre'])
		},
		{
			name: 'clear-then-send',
			knownBug: {
				issue: 797,
				// A race between the old and the new event connection: most runs hit it.
				intermittent: true,
				error: /^Error: reply never arrived: "You said: “after clearing"/,
				reason: 'after an in-place clear the server event stream skips the restarted log, so the reply never reaches the open chat'
			},
			expectShift: [
				{
					step: 'the server clears the open chat',
					composer: ['bottom', 'centre'],
					maxValue: 0.15,
					reason:
						'the clear comes from the server, not from input on this page: the composer moves once from the bottom to the centred cleared layout'
				}
			],
			// Docked, centred by the clear, docked again by the send: two moves.
			verify: ({ recording }) => expectComposerPositions(recording, ['bottom', 'centre', 'bottom']),
			async setup(ctx) {
				const { page } = ctx;
				ctx.old = `tok-${uniq()}`;
				await openNewChat(ctx);
				await typeAndSend(page, `before clearing ${ctx.old}`);
				await waitTurnSettled(page, { text: 'Fake AI here', messagesAtLeast: 2, hold: 500 });
				ctx.chatId = await chatIdByToken(page, ctx.old);
			},
			async act(ctx) {
				const { page, mark, variant, log } = ctx;
				await mark('the server clears the open chat');
				await api(page, 'POST', `/sessions/${encodeURIComponent(ctx.chatId)}/clear`);
				await expectClearedState(page, null);
				// The title keeps the old words (sidebar, header); the transcript must not.
				expect((await countUserRows(page, ctx.old)) === 0, 'the cleared messages are gone from the transcript');
				await page.waitForTimeout(1000);

				await mark('send in the cleared chat');
				const fresh = `tok-${uniq()}`;
				const t0 = Date.now();
				await typeAndSend(page, `after clearing ${fresh} ${fakeAiTag(variant)}`.trim());
				await waitTurnSettled(page, { text: 'You said: “after clearing', messagesAtLeast: 2, timeout: 30_000 });
				log(`reply settled ${Date.now() - t0}ms after send`);
				const state = await page.evaluate(() => document.querySelector('[data-ui-state="chat"]').getAttribute('data-ui-messages'));
				expect(Number(state) === 2, `a fresh conversation of 2 messages, not ${state}`);
				expect((await countUserRows(page, ctx.old)) === 0, 'no message from before the clear came back');
				expect((await countUserRows(page, fresh)) === 1, 'the new message shows once');
			}
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
});
