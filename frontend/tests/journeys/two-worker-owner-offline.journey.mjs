// Journey (two real workers): the worker that owns an open chat goes offline,
// then comes back.
//
// Worker A's UI shows a chat worker B owns. B's app is stopped for real
// (`python -m scripts.devloop pair offline b`: the process is gone, its
// database stays), as when a worker shuts down. A must notice (its network
// view, and the chat's runner chip says B is offline) while the transcript
// stays on screen; once B is back, A sees it online again without a reload
// and a message sent in the chat is answered by B.
import { runJourney } from './runner.mjs';
import { chatState, expect, gotoApp, openFromSidebar, typeAndSend, uniq, waitChatOpen, waitTurnSettled, watchUserText } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
import { PAIR_NOISE, PAIR_SEEDED, expectOwner, pairChatId, setWorker, transcriptOn, userMessagesWith, waitPeerOnline } from './lib/pair.mjs';
import { frames, markTime } from './lib/timeline.mjs';

const OFFLINE = 'worker B goes offline';
let sent = null;

const runnerChip = (page) => page.getByRole('toolbar', { name: 'Session context' }).getByRole('button', { name: /^Runner: Dev worker B/ });

await runJourney({
	name: 'two-worker-owner-offline',
	pair: true,
	viewports: ['desktop'],
	throttles: ['fast'],
	recorder: { selectors: ['rows=[data-ui-row]'] },
	cases: [
		{
			name: 'offline-and-back',
			async setup({ page, status }) {
				sent = null;
				await gotoApp(page, status);
				await openFromSidebar(page, PAIR_SEEDED.bLoose);
				await waitChatOpen(page, { messagesAtLeast: 12 });
				await runnerChip(page).waitFor({ timeout: 30_000 });
			},
			async act(ctx) {
				const { page, mark } = ctx;
				await mark(OFFLINE);
				try {
					await setWorker(ctx, 'b', 'offline');
					await waitPeerOnline(ctx, 'a', 'b', false, { timeout: 150_000 });
					await page
						.getByRole('toolbar', { name: 'Session context' })
						.getByRole('button', { name: 'Runner: Dev worker B (offline)' })
						.waitFor({ timeout: 60_000 });
				} finally {
					// Never leave B down for the next case or journey, even on a failure.
					await mark('worker B comes back');
					await setWorker(ctx, 'b', 'online');
				}
				await waitPeerOnline(ctx, 'a', 'b', true, { timeout: 150_000 });
				await page
					.getByRole('toolbar', { name: 'Session context' })
					.getByRole('button', { name: 'Runner: Dev worker B', exact: true })
					.waitFor({ timeout: 60_000 });
				sent = `after the outage ${uniq()}`;
				await mark('send once worker B is back');
				const before = Number((await chatState(page))?.messages || 0);
				const seen = await watchUserText(page, sent);
				await typeAndSend(page, sent);
				await waitTurnSettled(page, { messagesAtLeast: before + 2, timeout: 120_000 });
				const { max } = await seen();
				expect(max === 1, `the message showed ${max} times on some frame (want 1)`);
			},
			async verify(ctx) {
				const { recording } = ctx;
				// The transcript stayed on screen the whole time.
				const from = markTime(recording, OFFLINE);
				const gone = frames(recording).find(({ t, state }) => t >= from && Number(state.rows) < 12);
				if (gone) throw new Error(`the transcript dropped to ${gone.state.rows} rows at ${gone.t}ms while its owner was away`);
				const chat = pairChatId(ctx.pair.b, 'b_loose');
				await expectOwner(ctx, chat, 'b');
				const n = userMessagesWith(await transcriptOn(ctx, ctx.pair.b, chat), sent);
				if (n !== 1) throw new Error(`worker B's transcript has the message ${n} times (want 1)`);
			}
		}
	],
	allow: [...DEV_INSTANCE_NOISE, ...PAIR_NOISE]
});
